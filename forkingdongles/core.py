from glob import glob
import importlib
import inspect
import os.path
import sys

from twisted.enterprise.adbapi import ConnectionPool
from twisted.internet import defer, reactor, protocol
from twisted.python import log
from twisted.words.protocols import irc

from .plugin import Event, EventManager, PluginManager
from .utils import Channel, User

class ForkingDongles(irc.IRCClient):
    plugins = {}
    users = {}
    channels = {}
    reloads = 0
    state_delay = 1

    def connectionMade(self, *args, **kwargs):
        self.config = self.factory.config
        self.db = self.factory.db

        self.nickname = self.config['core']['nickname']

        super().connectionMade(*args, **kwargs)

    def signedOn(self):
        self.users = {}
        self.channels = {}

        self.event_manager = EventManager(self)
        self.event_manager.register(Event.RAW, 2)
        self.event_manager.register(Event.INVITE, 2)
        self.event_manager.register(Event.PRIVMSG, 3)
        self.event_manager.register(Event.NOTICE, 3)
        self.event_manager.register(Event.NICK, 2)
        self.event_manager.register(Event.MODE, 3)
        self.event_manager.register(Event.TOPIC, 3)
        self.event_manager.register(Event.JOIN, 2)
        self.event_manager.register(Event.PART, 3)
        self.event_manager.register(Event.KICK, 4)
        self.event_manager.register(Event.QUIT, 2)

        self.plugin_manager = PluginManager(self, [os.path.splitext(os.path.basename(x))[0] for x in glob('plugins/*.py')])

        for channel in self.config['core']['channels']:
            self.join(channel)

    def connectionLost(self, reason):
        self.config['core']['channels'] = self.channels.keys()

        super().connectionLost(reason)

    def addUser(self, user):
        if type(user) is not User:
            user = User(user)
            
        user.is_self = user.nick.lower() == self.nickname.lower()

        if user.nick.lower() not in self.users.keys():
            self.users[user.nick.lower()] = user
            self.whois(user.nick)

        return self.users[user.nick.lower()]

    def renameUser(self, user, new):
        if type(user) is not User:
            user = User(user)

        self.users[new.lower()] = user
        del self.users[user.nick.lower()]
        self.users[new.lower()].nick = new

    def removeUser(self, user):
        if type(user) is not User:
            user = User(user)

        if user.nick.lower() in self.users.keys():
            user = self.users.pop(user.nick.lower())
            
        return user

    def inChannel(self, channel):
        if type(channel) is Channel:
            channel = channel.name

        return channel.lower() in self.channels.keys()

    def joined(self, channel):
        if channel.lower() not in self.channels.keys():
            self.channels[channel.lower()] = Channel(channel)

        reactor.callLater(self.state_delay, self.sendLine, 'MODE {}'.format(channel))
        reactor.callLater(self.state_delay, self.sendLine, 'MODE {} b'.format(channel))

    def left(self, channel):
        if channel.lower() in self.channels.keys():
            del self.channels[channel.lower()]

    def kickedFrom(self, channel, kicker, message):
        if channel.lower() in self.channels.keys():
            del self.channels[channel.lower()]

    def userJoined(self, user, channel):
        user = self.addUser(user)
        self.channels[channel.lower()].addUser(user)

    def userLeft(self, user, channel):
        user = User(user)
        user = self.users[user.nick.lower()]
        self.channels[channel.lower()].removeUser(user)

        if True not in map(lambda channel: user.nick.lower() in channel.users, self.channels.values()):
            self.removeUser(user)

    def userQuit(self, user, message):
        user = self.removeUser(user)
        map(lambda channel: channel.removeUser(user), self.channels.values())

    def userRenamed(self, old, new):
        user = self.users[old.lower()]
        self.renameUser(user, new)
        map(lambda channel: channel.renameUser(user), self.channels.values())
            
    def userKicked(self, user, channel, kicker, message):
        user = User(user)
        user = self.users[user.nick.lower()]
        self.channels[channel.lower()].removeUser(user)

        if True not in map(lambda channel: user.nick.lower() in channel.users, self.channels.values()):
            self.removeUser(user)

    def modeChanged(self, user, channel, added, modes, args):
        if user is not None:
            nick, _, userhost = user.partition('!')
        else:
            nick = userhost = None

        # User mode
        if not channel.startswith('#'):
            if channel.lower() in self.users.keys():
                for x in range(len(modes)):
                    if args[x] is not None:
                        self.users[channel.lower()].setMode(added, modes[x], args[x])
                    else:
                        self.users[channel.lower()].setMode(added, modes[x])
        # Channel mode
        else:
            if channel.lower() in self.channels.keys():
                for x in range(len(modes)):
                    if args[x] is not None:
                        self.channels[channel.lower()].setMode(added, modes[x], args[x])
                    else:
                        self.channels[channel.lower()].setMode(added, modes[x])

    @defer.inlineCallbacks
    def privmsg(self, user, channel, message):
        lnick = user.partition('!')[0].lower()
        user = self.users[lnick] if lnick in self.users.keys() else User(user)

        if channel.lower() in self.channels.keys():
            channel = self.channels[channel.lower()]
        else:
            channel = Channel(user)

        result = yield self.plugin_manager.fire_command(user, channel, message)

        if result is not Event.STOP_ALL:
            yield self.event_manager.fire(Event.PRIVMSG, user, channel, message)

    # MODE #channel
    def irc_RPL_CHANNELMODEIS(self, prefix, params):
        channel, modes, args = params[1], params[2], params[3:]

        if modes[0] not in '-+':
            modes = '+' + modes

        paramModes = self.getChannelModeParams()

        try:
            added, removed = irc.parseModes(modes, args, paramModes)
        except irc.IRCBadModes:
            log.err(None, 'An error occurred while parsing the following MODE message: MODE {}'.format(' '.join(params)))
        else:
            modes, params = zip(*added)
            self.modeChanged(None, channel, True, ''.join(modes), params)

    def irc_INVITE(self, prefix, params):
        user = User(prefix)
        channel = Channel(params[1])

        if user.nick.lower() in self.users.keys():
            user = self.users[user.nick.lower()]

        if channel.name.lower() in self.channels.keys():
            channel = self.channels[channel.lower()]

        self.event_manager.fire(Event.INVITE, user, channel)

    # MODE #channel b
    def irc_RPL_BANLIST(self, prefix, params):
        channel, mask, args = params[1], params[2], params[3:]
        user = args.pop(0)
        self.modeChanged(user, channel, True, 'b', (mask,))

    # NAMES #channel
    def irc_RPL_NAMREPLY(self, prefix, params):
        channel, nicks = params[-2], params[-1]
        modes = ''
        args = []
        
        if not self.supported.hasFeature('PREFIX'):
            mode_translation = {
                '~': 'q',
                '&': 'a',
                '@': 'o',
                '%': 'h',
                '+': 'v'
            }
        else:
            mode_translation = dict(map(lambda i: (i[1][0], i[0]), self.supported.getFeature('PREFIX').items()))

        for nick in nicks.split():
            if nick[0] in mode_translation.keys():
                modes += mode_translation[nick[0]]
                args.append(nick[1:])
                nick = nick[1:]

            self.addUser(nick)
            self.channels[channel.lower()].addUser(nick)

        self.modeChanged(None, channel, True, modes, tuple(args))

    def irc_RPL_WHOISUSER(self, prefix, params):
        user = User('{}!{}@{}'.format(*params[1:4]))

        if user.nick.lower() not in self.users.keys():
            self.users[user.nick.lower()] = user
        else:
            self.users[user.nick.lower()].user = user.user
            self.users[user.nick.lower()].host = user.host

    def irc_RPL_WHOISOPERATOR(self, prefix, params):
        if params[1].lower() in self.users.keys():
            self.users[params[1].lower()].setMode(True, 'o')

    def irc_330(self, prefix, params):
        if params[1].lower() in self.users.keys():
            self.users[params[1].lower()].setMode(True, 'r')

    def irc_671(self, prefix, params):
        if params[1].lower() in self.users.keys():
            self.users[params[1].lower()].setMode(True, 'z')

class ForkingDonglesFactory(protocol.Factory):
    protocol = ForkingDongles

    def __init__(self, config, db):
        self.config = config
        self.db = db
