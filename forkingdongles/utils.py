import copy
from cgi import parse_header
from functools import reduce, wraps
from heapq import nsmallest
from io import BytesIO
import json
from operator import itemgetter
import os.path

from lxml.html import document_fromstring
from PIL import Image

from twisted.internet import defer, protocol, reactor
from twisted.web.client import Agent, RedirectAgent, ContentDecoderAgent, GzipDecoder
from twisted.web.http_headers import Headers
from twisted.web.iweb import UNKNOWN_LENGTH

class JSONConfig(object):
    config = {} 

    def __init__(self, filename='config.json', default=None):
        self.filename = filename

        if not os.path.isfile(self.filename) and default is not None:
            self.config = default
            self.save()
        else:
            self.load()

    def __getitem__(self, key):
        return self.config[key]

    def __setitem__(self, key, value):
        self.config[key] = value

    def __delitem__(self, key):
        del self.config[key]

    def load(self):
        with open(self.filename, 'r') as fd:
            self.config = json.load(fd)

    def save(self):
        with open(self.filename, 'w') as fd:
            json.dump(self.config, fd, indent='  ', sort_keys=True)

class User:
    def __init__(self, mask, modes='', is_self=False):
        self.nick, _, mask = mask.partition('!')
        self.user, _, self.host = mask.partition('@')
        self.modes = modes
        self.is_self = is_self

    def __str__(self):
        return self.nick

    def isIdentified(self):
        return 'r' in self.modes

    def isSecure(self):
        return 'z' in self.modes

    def isOper(self):
        return 'o' in self.modes

    def setMode(self, added, mode, arg=None):
        if added and mode not in self.modes:
            self.modes += mode
        elif not added and mode in self.modes:
            self.modes = self.modes.replace(mode, '')

class Channel:
    def __init__(self, name, modes=''):
        self.name = str(name)
        self.users = []
        self.modes = modes
        
        for c in list('qsahv'):
            setattr(self, c + 'ops', [])

        self.bans = []
        self.isUser = not self.name.startswith('#')

    def __str__(self):
        return self.name

    def isQOP(self, user):
        if type(user) is User:
            user = user.nick

        return user.lower() in self.qops

    def isSOP(self, user):
        if type(user) is User:
            user = user.nick

        return user.lower() in self.sops

    def isAOP(self, user):
        if type(user) is User:
            user = user.nick

        return user.lower() in self.aops

    isOP = isAOP

    def isHOP(self, user):
        if type(user) is User:
            user = user.nick

        return user.lower() in self.hops

    isHalfOP = isHOP

    def isVOP(self, user):
        if type(user) is User:
            user = user.nick

        return user.lower() in self.vops

    isVoice = isVOP

    def addUser(self, user):
        if type(user) is User:
            user = user.nick

        if user.lower() not in self.users:
            self.users.append(user.lower())

    def renameUser(self, user, new):
        if type(user) is User:
            user = user.nick
            
        for attr in ['users', 'qops', 'sops', 'aops', 'hops', 'vops']:
            if hasattr(self, attr) and user.lower() in getattr(self, attr):
                getattr(self, attr).remove(user.lower())
                getattr(self, attr).append(new.lower())

    def removeUser(self, user):
        if type(user) is User:
            user = user.nick

        # The user is leaving the channel so they're not here at all
        for attr in ['users', 'qops', 'sops', 'aops', 'hops', 'vops']:
            if hasattr(self, attr) and user.lower() in getattr(self, attr):
                getattr(self, attr).remove(user.lower())

    def setMode(self, added, mode, arg=None):
        op_translations = {
            'q': 'q',
            'a': 's',
            'o': 'a',
            'h': 'h',
            'v': 'v'
        }

        if added:
            if arg is not None:
                if mode in op_translations.keys() and hasattr(self, op_translations[mode] + 'ops') and arg.lower() not in getattr(self, op_translations[mode] + 'ops'):
                    getattr(self, op_translations[mode] + 'ops').append(arg.lower())
                elif mode == 'b' and arg.lower() not in self.bans:
                    self.bans.append(arg.lower())
            elif mode not in self.modes:
                self.modes += mode
        else:
            if arg is not None:
                if mode in op_translations.keys() and hasattr(self, op_translations[mode] + 'ops') and arg.lower() in getattr(self, op_translations[mode] + 'ops'):
                    getattr(self, op_translations[mode] + 'ops').remove(arg.lower())
                elif mode == 'b' and arg.lower() in self.bans:
                    self.bans.remove(arg.lower())
            elif mode in self.modes:
                self.modes = self.modes.replace(mode, '')

class Receiver(protocol.Protocol):
    def __init__(self, response, finished):
        self.response = response
        self.charset = response.headers['Content-Type'][1]['charset']
        self.finished = finished
        self.buffer = ''
        self.remaining = 1024 * 1024 * 4

        if self.response.length is UNKNOWN_LENGTH:
            self.response.length = 0
            self.obtain_length = True
        else:
            self.obtain_length = False
    
    def dataReceived(self, data):
        if self.remaining > 0:
            data = data[:self.remaining]
            self.buffer += data.decode(self.charset)
            self.remaining -= len(data)

            if self.obtain_length:
                self.response.length += len(data)

    def connectionLost(self, reason):
        self.finished.callback(self.buffer)

class ImageReceiver(Receiver):
    def __init__(self, response, finished):
        super().__init__(response, finished)
        self.buffer = BytesIO()

    def dataReceived(self, data):
        if self.remaining > 0:
            data = data[:self.remaining]
            self.buffer.write(data)
            self.remaining -= len(data)

            if self.obtain_length:
                self.response.length += len(data)

    def connectionLost(self, reason):
        # Return the image since it's the body
        self.finished.callback(Image.open(self.buffer))

class HTMLReceiver(Receiver):
    def connectionLost(self, reason):
        self.finished.callback(document_fromstring(self.buffer))

class JSONReceiver(Receiver):
    def connectionLost(self, reason):
        self.finished.callback(json.loads(self.buffer))

# Inspired by cyclone.httpclient.HTTPClient
class HTTPClient:
    user_agent = b'Mozilla/5.0 (Windows NT 6.2; WOW64; rv:28.0) Gecko/20100101 Firefox/28.0'

    def __init__(self):
        self.agent = ContentDecoderAgent(RedirectAgent(Agent(reactor)), [(b'gzip', GzipDecoder)])

    @defer.inlineCallbacks
    def fetch(self, url, receiver=None):
        resp = yield self.agent.request(b'GET', url.encode('utf-8'), Headers({b'User-Agent': [self.user_agent]}))
        resp.error = None
        resp.headers = dict(resp.headers.getAllRawHeaders()) 

        for k, v in resp.headers.copy().items():
            resp.headers[k.decode('utf-8')] = parse_header(v[0].decode('utf-8'))
            del resp.headers[k]

        if 'Content-Type' not in resp.headers.keys():
            resp.headers['Content-Type'] = ['application/octet-stream', {'charset': 'utf-8'}]
        elif 'charset' not in resp.headers['Content-Type'][1].keys():
                resp.headers['Content-Type'][1]['charset'] = 'utf-8'

        if receiver is None:
            mime_type = resp.headers['Content-Type'][0]
            if mime_type.startswith('image'):
                receiver = ImageReceiver
            elif mime_type == 'application/json':
                receiver = JSONReceiver
            elif mime_type == 'text/html':
                receiver = HTMLReceiver
            else:
                receiver = Receiver

        d = defer.Deferred()
        resp.receiver = receiver
        resp.deliverBody(resp.receiver(resp, d))
        resp.body = yield d

        if resp.length is UNKNOWN_LENGTH:
            resp.length = None # A null value serves as a good unknown

        defer.returnValue(resp)

# Thanks e000 for the original snippet of this code
def deferred_lfu_cache(maxsize=100):
    class Counter(dict):
        def __missing__(self, key):
            return 0

    def decorator(func):
        cache = {}
        use_count = Counter()
        kwargs_mark = object()
        waiting = {}
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = args

            if kwargs:
                key += (kwargs_mark,) + tuple(sorted(kwargs.items()))

            use_count[key] += 1

            if key in cache:
                wrapper.hits += 1
                return defer.succeed((cache[key], True))
            elif key in waiting:
                d = defer.Deferred()
                waiting[key].append(d)
                return d
            else:
                def success(result, key):
                    wrapper.misses += 1
                    cache[key] = result
                    
                    if len(cache) > maxsize:
                        for key, _ in nsmallest(maxsize / 10, use_count.items(), key=itemgetter(1)):
                            del cache[key], use_count[key]

                    wrapper.hits -= 1

                    for d in waiting[key]:
                        wrapper.hits += 1
                        d.callback((result, False))

                    del waiting[key]

                def error(err, key):
                    for d in waiting[key]:
                        d.errback(err)
                    
                    del waiting[key]

                defer.maybeDeferred(func, *args, **kwargs).addCallback(success, key).addErrback(error, key)

                d = defer.Deferred()
                waiting[key] = [d]
                return d

        def clear():
            cache.clear()
            use_count.clear()
            wrapper.hits = wrapper.misses = 0

        wrapper.hits = wrapper.misses = 0
        wrapper.clear = clear
        wrapper.size = lambda: len(cache)
        wrapper.waiting = lambda: len(waiting)
        wrapper.maxsize = maxsize

        return wrapper
    return decorator
