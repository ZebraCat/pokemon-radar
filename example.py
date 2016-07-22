#!/usr/bin/python
# -*- coding: utf-8 -*-

import flask
from flask import Flask, render_template, request
from multiprocessing.pool import ThreadPool
from flask_googlemaps import GoogleMaps
from flask_googlemaps import Map
from flask_googlemaps import icons
import os
import re
import sys
import struct
import json
import requests
import argparse
import getpass
import pokemon_pb2
import time
from google.protobuf.internal import encoder
from google.protobuf.message import DecodeError
from s2sphere import *
from datetime import datetime
from gpsoauth import perform_master_login, perform_oauth
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from requests.adapters import ConnectionError
from requests.models import InvalidURL
from transform import *

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

API_URL = 'https://pgorelease.nianticlabs.com/plfe/rpc'
LOGIN_URL = \
    'https://sso.pokemon.com/sso/login?service=https://sso.pokemon.com/sso/oauth2.0/callbackAuthorize'
LOGIN_OAUTH = 'https://sso.pokemon.com/sso/oauth2.0/accessToken'
APP = 'com.nianticlabs.pokemongo'

with open('credentials.json') as file:
	credentials = json.load(file)

PTC_CLIENT_SECRET = credentials.get('ptc_client_secret', None)
ANDROID_ID = credentials.get('android_id', None)
SERVICE = credentials.get('service', None)
CLIENT_SIG = credentials.get('client_sig', None)
GOOGLEMAPS_KEY = credentials.get('gmaps_key', None)

SESSION = requests.session()
SESSION.headers.update({'User-Agent': 'Niantic App'})
SESSION.verify = False

global_password = None
global_token = None
access_token = None
DEBUG = True
VERBOSE_DEBUG = False  # if you want to write raw request/response to the console
auto_refresh = 0
default_step = 0.001
api_endpoint = None
pokemons = {}
gyms = {}
pokestops = {}
numbertoteam = {  # At least I'm pretty sure that's it. I could be wrong and then I'd be displaying the wrong owner team of gyms.
    0: 'Gym',
    1: 'Mystic',
    2: 'Valor',
    3: 'Instinct',
}
is_ampm_clock = False

# stuff for in-background search thread

search_thread = None

def memoize(obj):
    cache = obj.cache = {}

    @functools.wraps(obj)
    def memoizer(*args, **kwargs):
        key = str(args) + str(kwargs)
        if key not in cache:
            cache[key] = obj(*args, **kwargs)
        return cache[key]
    return memoizer

def parse_unicode(bytestring):
    decoded_string = bytestring.decode(sys.getfilesystemencoding())
    return decoded_string


def debug(message):
    if DEBUG:
        print '[-] {}'.format(message)


def time_left(ms):
    s = ms / 1000
    (m, s) = divmod(s, 60)
    (h, m) = divmod(m, 60)
    return (h, m, s)


def encode(cellid):
    output = []
    encoder._VarintEncoder()(output.append, cellid)
    return ''.join(output)


def getNeighbors(float_lat, float_long):
    origin = CellId.from_lat_lng(LatLng.from_degrees(float_lat,
                                                     float_long)).parent(15)
    walk = [origin.id()]

    # 10 before and 10 after

    next = origin.next()
    prev = origin.prev()
    for i in range(10):
        walk.append(prev.id())
        walk.append(next.id())
        next = next.next()
        prev = prev.prev()
    return walk


def f2i(float):
    return struct.unpack('<Q', struct.pack('<d', float))[0]


def f2h(float):
    return hex(struct.unpack('<Q', struct.pack('<d', float))[0])


def h2f(hex):
    return struct.unpack('<d', struct.pack('<Q', int(hex, 16)))[0]

def retrying_api_req(service, api_endpoint, access_token, location_coords,*args, **kwargs):
    while True:
        try:
            response = api_req(service, api_endpoint, access_token, location_coords, *args,
                               **kwargs)
            if response:
                return response
            debug('retrying_api_req: api_req returned None, retrying')
        except (InvalidURL, ConnectionError, DecodeError), e:
            debug('retrying_api_req: request error ({}), retrying'.format(
                str(e)))
        time.sleep(1)


def api_req(service, api_endpoint, access_token, location_coords, *args, **kwargs):
    p_req = pokemon_pb2.RequestEnvelop()
    p_req.rpc_id = 1469378659230941192

    p_req.unknown1 = 2

    (p_req.latitude, p_req.longitude, p_req.altitude) = \
        tuple(map(f2i, location_coords))
    p_req.unknown12 = 989

    if 'useauth' not in kwargs or not kwargs['useauth']:
        p_req.auth.provider = service
        p_req.auth.token.contents = access_token
        p_req.auth.token.unknown13 = 14
    else:
        p_req.unknown11.unknown71 = kwargs['useauth'].unknown71
        p_req.unknown11.unknown72 = kwargs['useauth'].unknown72
        p_req.unknown11.unknown73 = kwargs['useauth'].unknown73

    for arg in args:
        p_req.MergeFrom(arg)

    protobuf = p_req.SerializeToString()

    r = SESSION.post(api_endpoint, data=protobuf, verify=False)

    p_ret = pokemon_pb2.ResponseEnvelop()
    p_ret.ParseFromString(r.content)

    if VERBOSE_DEBUG:
        print 'REQUEST:'
        print p_req
        print 'Response:'
        print p_ret
        print '''

'''
    time.sleep(0.51)
    return p_ret


def get_api_endpoint(service, access_token, location_coords, api=API_URL):
    profile_response = None
    while not profile_response:
        profile_response = retrying_get_profile(service, access_token, api, None,
                                                location_coords)
        if not hasattr(profile_response, 'api_url'):
            debug(
                'retrying_get_profile: get_profile returned no api_url, retrying')
            profile_response = None
            continue
        if not len(profile_response.api_url):
            debug(
                'get_api_endpoint: retrying_get_profile returned no-len api_url, retrying')
            profile_response = None

    return 'https://%s/rpc' % profile_response.api_url

def retrying_get_profile(service, access_token, api, useauth, location_coords,*reqq):
    profile_response = None
    while not profile_response:
        profile_response = get_profile(service, access_token, api, useauth, location_coords,
                                       *reqq)
        if not hasattr(profile_response, 'payload'):
            debug(
                'retrying_get_profile: get_profile returned no payload, retrying')
            profile_response = None
            continue
        if not profile_response.payload:
            debug(
                'retrying_get_profile: get_profile returned no-len payload, retrying')
            profile_response = None

    return profile_response

def get_profile(service, access_token, api, useauth, location_coords, *reqq):
    req = pokemon_pb2.RequestEnvelop()
    req1 = req.requests.add()
    req1.type = 2
    if len(reqq) >= 1:
        req1.MergeFrom(reqq[0])

    req2 = req.requests.add()
    req2.type = 126
    if len(reqq) >= 2:
        req2.MergeFrom(reqq[1])

    req3 = req.requests.add()
    req3.type = 4
    if len(reqq) >= 3:
        req3.MergeFrom(reqq[2])

    req4 = req.requests.add()
    req4.type = 129
    if len(reqq) >= 4:
        req4.MergeFrom(reqq[3])

    req5 = req.requests.add()
    req5.type = 5
    if len(reqq) >= 5:
        req5.MergeFrom(reqq[4])
    return retrying_api_req(service, api, access_token, location_coords, req, useauth=useauth)

def login_google(username, password):
    print '[!] Google login for: {}'.format(username)
    r1 = perform_master_login(username, password, ANDROID_ID)
    r2 = perform_oauth(username,
                       r1.get('Token', ''),
                       ANDROID_ID,
                       SERVICE,
                       APP,
                       CLIENT_SIG, )
    return r2.get('Auth')

def login_ptc(username, password):
    print '[!] PTC login for: {}'.format(username)
    head = {'User-Agent': 'Niantic App'}
    r = SESSION.get(LOGIN_URL, headers=head)
    if r is None:
        return render_template('nope.html', fullmap=fullmap)

    try:
        jdata = json.loads(r.content)
    except ValueError, e:
        debug('login_ptc: could not decode JSON from {}'.format(r.content))
        return None

    # Maximum password length is 15 (sign in page enforces this limit, API does not)

    if len(password) > 15:
        print '[!] Trimming password to 15 characters'
        password = password[:15]

    data = {
        'lt': jdata['lt'],
        'execution': jdata['execution'],
        '_eventId': 'submit',
        'username': username,
        'password': password,
    }
    r1 = SESSION.post(LOGIN_URL, data=data, headers=head)

    ticket = None
    try:
        ticket = re.sub('.*ticket=', '', r1.history[0].headers['Location'])
    except Exception, e:
        if DEBUG:
            print r1.json()['errors'][0]
        return None

    data1 = {
        'client_id': 'mobile-app_pokemon-go',
        'redirect_uri': 'https://www.nianticlabs.com/pokemongo/error',
        'client_secret': PTC_CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'code': ticket,
    }
    r2 = SESSION.post(LOGIN_OAUTH, data=data1)
    access_token = re.sub('&expires.*', '', r2.content)
    access_token = re.sub('.*access_token=', '', access_token)

    return access_token


def get_heartbeat(service,
                  api_endpoint,
                  access_token,
                  response, float_lat, float_long):
    m4 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleInt()
    m.f1 = int(time.time() * 1000)
    m4.message = m.SerializeToString()
    m5 = pokemon_pb2.RequestEnvelop.Requests()
    m = pokemon_pb2.RequestEnvelop.MessageSingleString()
    m.bytes = '05daf51635c82611d1aac95c0b051d3ec088a930'
    m5.message = m.SerializeToString()
    walk = sorted(getNeighbors(float_lat, float_long))
    m1 = pokemon_pb2.RequestEnvelop.Requests()
    m1.type = 106
    m = pokemon_pb2.RequestEnvelop.MessageQuad()
    m.f1 = ''.join(map(encode, walk))
    m.f2 = \
        "\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000"
    m.lat = f2i(float_lat)
    m.long = f2i(float_long)
    m1.message = m.SerializeToString()
    response = get_profile(service,
                           access_token,
                           api_endpoint,
                           response.unknown7,
                           (float_lat, float_long, 0),
                           m1,
                           pokemon_pb2.RequestEnvelop.Requests(),
                           m4,
                           pokemon_pb2.RequestEnvelop.Requests(),
                           m5, )

    try:
        payload = response.payload[0]
    except (AttributeError, IndexError):
        return

    heartbeat = pokemon_pb2.ResponseEnvelop.HeartbeatPayload()
    heartbeat.ParseFromString(payload)
    return heartbeat

def get_token(service, username, password):
    """
    Get token if it's not None
    :return:
    :rtype:
    """

    global global_token
    if global_token is None:
        if service == 'ptc':
            global_token = login_ptc(username, password)
        else:
            global_token = login_google(username, password)
        return global_token
    else:
        return global_token


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-a', '--auth_service', type=str.lower, help='Auth Service', default='ptc')
    parser.add_argument('-u', '--username', help='Username', required=True)
    parser.add_argument('-p', '--password', help='Password', required=False)
    parser.add_argument(
        '-l', '--location', type=parse_unicode, help='Location', required=True)
    parser.add_argument('-st', '--step-limit', help='Steps', required=True)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        '-i', '--ignore', help='Comma-separated list of Pokémon names or IDs to ignore')
    group.add_argument(
        '-o', '--only', help='Comma-separated list of Pokémon names or IDs to search')
    parser.add_argument(
        "-ar",
        "--auto_refresh",
        help="Enables an autorefresh that behaves the same as a page reload. " +
             "Needs an integer value for the amount of seconds")
    parser.add_argument(
        '-dp',
        '--display-pokestop',
        help='Display pokéstop',
        action='store_true',
        default=False)
    parser.add_argument(
        '-dg',
        '--display-gym',
        help='Display Gym',
        action='store_true',
        default=False)
    parser.add_argument(
        '-H',
        '--host',
        help='Set web server listening host',
        default='127.0.0.1')
    parser.add_argument(
        '-P',
        '--port',
        type=int,
        help='Set web server listening port',
        default=5000)
    parser.add_argument(
        "-L",
        "--locale",
        help="Locale for Pokemon names: default en, check locale folder for more options",
        default="en")
    parser.add_argument(
        "-ol",
        "--onlylure",
        help='Display only lured pokéstop',
        action='store_true')
    parser.add_argument(
        '-c',
        '--china',
        help='Coordinates transformer for China',
        action='store_true')
    parser.add_argument(
    	"-pm",
    	"--ampm_clock",
    	help="Toggles the AM/PM clock for Pokemon timers",
    	action='store_true',
    	default=False)
    parser.add_argument(
        '-d', '--debug', help='Debug Mode', action='store_true')
    parser.set_defaults(DEBUG=True)
    return parser.parse_args()

@memoize
def login(args, location_coords):
    global global_password
    if not global_password:
      if args.password:
        global_password = args.password
      else:
        global_password = getpass.getpass()

    access_token = get_token(args.auth_service, args.username, global_password)
    if access_token is None:
        raise Exception('[-] Wrong username/password')

    print '[+] RPC Session Token: {} ...'.format(access_token[:25])

    api_endpoint = get_api_endpoint(args.auth_service, access_token, location_coords)
    if api_endpoint is None:
        raise Exception('[-] RPC server offline')

    print '[+] Received API endpoint: {}'.format(api_endpoint)

    profile_response = retrying_get_profile(args.auth_service, access_token,
                                            api_endpoint, None, location_coords)
    if profile_response is None or not profile_response.payload:
        raise Exception('Could not get profile')

    print '[+] Login successful'

    payload = profile_response.payload[0]
    profile = pokemon_pb2.ResponseEnvelop.ProfilePayload()
    profile.ParseFromString(payload)
    print '[+] Username: {}'.format(profile.profile.username)

    creation_time = \
        datetime.fromtimestamp(int(profile.profile.creation_time)
                               / 1000)
    print '[+] You started playing Pokemon Go on: {}'.format(
        creation_time.strftime('%Y-%m-%d %H:%M:%S'))

    for curr in profile.profile.currency:
        print '[+] {}: {}'.format(curr.type, curr.amount)

    return api_endpoint, access_token, profile_response




def process_step(args, api_endpoint, access_token, profile_response,
                 pokemonsJSON, ignore, only, float_lat, float_long):
    print('[+] Searching for Pokemon at location {} {}'.format(float_lat, float_long))
    parent = CellId.from_lat_lng(LatLng.from_degrees(float_lat,
                                                     float_long)).parent(15)
    h = get_heartbeat(args.auth_service, api_endpoint, access_token,
                      profile_response, float_lat, float_long)
    hs = [h]
    seen = set([])
    pokemons = {}

    for child in parent.children():
        latlng = LatLng.from_point(Cell(child).get_center())
        hs.append(
            get_heartbeat(args.auth_service, api_endpoint, access_token,
                          profile_response, latlng.lat().degrees, latlng.lng().degrees))

    visible = []

    for hh in hs:
        try:
            for cell in hh.cells:
                for wild in cell.WildPokemon:
                    hash = wild.SpawnPointId + ':' \
                        + str(wild.pokemon.PokemonId)
                    if hash not in seen:
                        visible.append(wild)
                        seen.add(hash)
                if cell.Fort:
                    for Fort in cell.Fort:
                        if Fort.Enabled == True:
                            if args.china:
                                (Fort.Latitude, Fort.Longitude) = \
transform_from_wgs_to_gcj(Location(Fort.Latitude, Fort.Longitude))
                            if Fort.GymPoints and args.display_gym:
                                gyms[Fort.FortId] = [Fort.Team, Fort.Latitude,
                                                     Fort.Longitude, Fort.GymPoints]

                            elif Fort.FortType \
                                and args.display_pokestop:
                                expire_time = 0
                                if Fort.LureInfo.LureExpiresTimestampMs:
                                    expire_time = datetime\
                                        .fromtimestamp(Fort.LureInfo.LureExpiresTimestampMs / 1000.0)\
                                        .strftime("%H:%M:%S")
                                if (expire_time != 0 or not args.onlylure):
                                    pokestops[Fort.FortId] = [Fort.Latitude,
                                                              Fort.Longitude, expire_time]
        except AttributeError:
            break

    for poke in visible:
        pokeid = str(poke.pokemon.PokemonId)
        pokename = pokemonsJSON[pokeid]
        if args.ignore:
            if pokename.lower() in ignore or pokeid in ignore:
                continue
        elif args.only:
            if pokename.lower() not in only and pokeid not in only:
                continue

        disappear_timestamp = time.time() + poke.TimeTillHiddenMs \
            / 1000

        if args.china:
            (poke.Latitude, poke.Longitude) = \
                transform_from_wgs_to_gcj(Location(poke.Latitude,
                    poke.Longitude))

        pokemons[poke.SpawnPointId] = {
            "lat": poke.Latitude,
            "lng": poke.Longitude,
            "disappear_time": disappear_timestamp,
            "id": poke.pokemon.PokemonId,
            "name": pokename
        }
    return pokemons

def clear_stale_pokemons():
    current_time = time.time()

    for pokemon_key in pokemons.keys():
        pokemon = pokemons[pokemon_key]
        if current_time > pokemon['disappear_time']:
            print "[+] removing stale pokemon %s at %f, %f from list" % (
                pokemon['name'].encode('utf-8'), pokemon['lat'], pokemon['lng'])
            del pokemons[pokemon_key]


def create_app():
    app = Flask(__name__, template_folder='templates')

    GoogleMaps(app, key=GOOGLEMAPS_KEY)
    return app


app = create_app()


@app.route('/data')
def data():
    """ Gets all the PokeMarkers via REST """
    return json.dumps(get_pokemarkers())

@app.route('/raw_data')
def raw_data():
    """ Gets raw data for pokemons/gyms/pokestops via REST """
    return flask.jsonify(pokemons=pokemons, gyms=gyms, pokestops=pokestops)


@app.route('/config')
def config(float_lat, float_long):
    """ Gets the settings for the Google Maps via REST"""
    center = {
        'lat': float_lat,
        'lng': float_long,
        'zoom': 15,
        'identifier': "fullmap"
    }
    return json.dumps(center)


@app.route('/')
def fullmap():
    clear_stale_pokemons()

    return render_template(
        'example_fullmap.html', key=GOOGLEMAPS_KEY, fullmap=get_map(0.0, 0.0), auto_refresh=auto_refresh)

@app.route('/loc')
def O_fullmap_for_location():
    full_path = os.path.realpath(__file__)
    (path, filename) = os.path.split(full_path)
    latitude = float(request.args.get('lat', '0'))
    longitude = float(request.args.get('long', '0'))
    args = get_args()
    if args.auto_refresh:
        global auto_refresh
        auto_refresh = int(args.auto_refresh) * 1000

    if args.ampm_clock:
        global is_ampm_clock
        is_ampm_clock = True

    api_endpoint, access_token, profile_response = login(args, (latitude, longitude, 0))

    clear_stale_pokemons()

    pokemonsJSON = json.load(
        open(path + '/locales/pokemon.' + args.locale + '.json'))

    steplimit = int(args.step_limit)

    pos = 1
    x = 0
    y = 0
    dx = 0
    dy = -1
    steplimit2 = steplimit**2
    pool = ThreadPool(processes=4)
    results = []
    pokemons = {}
    for _ in range(steplimit2):
        if -steplimit2 / 2 < x <= steplimit2 / 2 and -steplimit2 / 2 < y <= steplimit2 / 2:
            latitude = x * 0.0025 + latitude
            longitude = y * 0.0025 + longitude
        if x == y or x < 0 and x == -y or x > 0 and x == 1 - y:
            (dx, dy) = (-dy, dx)

        (x, y) = (x + dx, y + dy)

        async_result = pool.apply_async(process_step, (args, api_endpoint, access_token, profile_response, pokemonsJSON, [], [], latitude, longitude))
        results.append(async_result)

    for i in range(steplimit2):
        pokemons.update(results[i].get())

    return render_template(
        'example_fullmap.html', key=GOOGLEMAPS_KEY, fullmap=OO_get_map(float(latitude), float(longitude), pokemons), auto_refresh=auto_refresh)


@app.route('/next_loc')
def next_loc():
    global NEXT_LAT, NEXT_LONG

    lat = flask.request.args.get('lat', '')
    lon = flask.request.args.get('lon', '')
    if not (lat and lon):
        print('[-] Invalid next location: %s,%s' % (lat, lon))
    else:
        print('[+] Saved next location as %s,%s' % (lat, lon))
        NEXT_LAT = float(lat)
        NEXT_LONG = float(lon)
        return 'ok'


def get_pokemarkers(latitude, longitude, pokemons):
    pokeMarkers = [{
        'icon': icons.dots.red,
        'lat': latitude,
        'lng': longitude,
        'infobox': "Start position",
        'type': 'custom',
        'key': 'start-position',
        'disappear_time': -1
    }]

    for pokemon_key in pokemons:
        pokemon = pokemons[pokemon_key]
        datestr = datetime.fromtimestamp(pokemon[
            'disappear_time'])
        dateoutput = datestr.strftime("%H:%M:%S")
        if is_ampm_clock:
            dateoutput = datestr.strftime("%I:%M%p").lstrip('0')
        pokemon['disappear_time_formatted'] = dateoutput

        LABEL_TMPL = u'''
<div><b>{name}</b><span> - </span><small><a href='http://www.pokemon.com/us/pokedex/{id}' target='_blank' title='View in Pokedex'>#{id}</a></small></div>
<div>Disappears at - {disappear_time_formatted} <span class='label-countdown' disappears-at='{disappear_time}'></span></div>
<div><a href='https://www.google.com/maps/dir/Current+Location/{lat},{lng}' target='_blank' title='View in Maps'>Get Directions</a></div>
'''
        label = LABEL_TMPL.format(**pokemon)
        #  NOTE: `infobox` field doesn't render multiple line string in frontend
        label = label.replace('\n', '')

        pokeMarkers.append({
            'type': 'pokemon',
            'key': pokemon_key,
            'disappear_time': pokemon['disappear_time'],
            'icon': 'static/icons/%d.png' % pokemon["id"],
            'lat': pokemon["lat"],
            'lng': pokemon["lng"],
            'infobox': label
        })

    for gym_key in gyms:
        gym = gyms[gym_key]
        if gym[0] == 0:
            color = "rgba(0,0,0,.4)"
        if gym[0] == 1:
            color = "rgba(74, 138, 202, .6)"
        if gym[0] == 2:
            color = "rgba(240, 68, 58, .6)"
        if gym[0] == 3:
            color = "rgba(254, 217, 40, .6)"

        icon = 'static/forts/'+numbertoteam[gym[0]]+'_large.png'
        pokeMarkers.append({
            'icon': 'static/forts/' + numbertoteam[gym[0]] + '.png',
            'type': 'gym',
            'key': gym_key,
            'disappear_time': -1,
            'lat': gym[1],
            'lng': gym[2],
            'infobox': "<div><center><small>Gym owned by:</small><br><b style='color:" + color + "'>Team " + numbertoteam[gym[0]] + "</b><br><img id='" + numbertoteam[gym[0]] + "' height='100px' src='"+icon+"'><br>Prestige: " + str(gym[3]) + "</center>"
        })
    for stop_key in pokestops:
        stop = pokestops[stop_key]
        if stop[2] > 0:
            pokeMarkers.append({
                'type': 'lured_stop',
                'key': stop_key,
                'disappear_time': -1,
                'icon': 'static/forts/PstopLured.png',
                'lat': stop[0],
                'lng': stop[1],
                'infobox': 'Lured Pokestop, expires at ' + stop[2],
            })
        else:
            pokeMarkers.append({
                'type': 'stop',
                'key': stop_key,
                'disappear_time': -1,
                'icon': 'static/forts/Pstop.png',
                'lat': stop[0],
                'lng': stop[1],
                'infobox': 'Pokestop',
            })
    return pokeMarkers


def get_map(lat, long):
    fullmap = Map(
        identifier="fullmap2",
        style='height:100%;width:100%;top:0;left:0;position:absolute;z-index:200;',
        lat=lat,
        lng=long,
        markers=get_pokemarkers(lat, long),
        zoom='15', )
    return fullmap

def OO_get_map(lat, lng, pokemons):
    return Map(
        identifier="fullmap2",
        style='height:100%;width:100%;top:0;left:0;position:absolute;z-index:200;',
        lat=lat,
        lng=lng,
        markers=get_pokemarkers(lat, lng, pokemons),
        zoom='15', )


if __name__ == '__main__':
    args = get_args()
    #register_background_thread(initial_registration=True)
    app.run(debug=True, threaded=True, host=args.host, port=args.port)
