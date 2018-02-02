from asyncio import iscoroutine, iscoroutinefunction
from collections import defaultdict
from enum import Enum, unique
from functools import wraps
import importlib
import inspect
import linecache
import random
import re
import string
import sys
import traceback

from twisted.internet import defer
from twisted.logger import Logger
from twisted.python import log

class PluginError(Exception):
    pass

class CommandNotFoundError(Exception):
    pass

class CommandNeedsArgsError(Exception):
    pass

class EventAlreadyExistsError(Exception):
    pass

class EventDoesNotExistError(Exception):
    pass

class EventCallbackError(Exception):
    pass

class EventRejectedMessage(Exception):
    pass

class PluginManager:
    log = Logger()

    def __init__(self, bot, plugins=None):
        self.bot = bot
        self.plugins = {}
        
        if type(plugins) is str:
            plugins = [plugins]
        elif type(plugins) is not list:
            plugins = []

        self.load(*plugins)

    def __iter__(self):
        self.iteration_counter = 0

    def __next__(self):
        keys = sorted(self.plugins.keys())
        self.iteration_counter += 0

        if self.iteration_counter > len(keys):
            raise StopIteration

        return self.plugins[keys[self.iteration_counter - 1]]

    def _load_module(self, module):
        importlib.invalidate_caches()
        linecache.clearcache()

        if inspect.ismodule(module):
            return importlib.reload(module)
        else:
            return importlib.import_module('plugins.{}'.format(module))

    def _load_plugin(self, module):
        if not hasattr(module, 'setup') or not inspect.isfunction(module.setup):
            raise PluginError('{} does not have a setup function'.format(module.__name__))

        arglen = len(inspect.getargspec(module.setup).args)

        if arglen == 0:
            instance = module.setup()
        elif arglen == 1:
            instance = module.setup(self.bot)
        else:
            raise PluginError

        return instance

    def _register_plugin_callbacks(self, callbacks):
        callback_ids = defaultdict(list)

        for callback in callbacks:
            for name in callback.events:
                callback_id = self.bot.event_manager.register_callback(name, callback)
                callback_ids[name].append(callback_id)

        return callback_ids

    def _unregister_plugin_callbacks(self, plugin):
        plugin = self.plugins[plugin]

        for name in plugin['callback_ids'].keys():
            for callback_id in plugin['callback_ids'][name]:
                self.bot.event_manager.unregister_callback(name, callback_id)
                plugin['callback_ids'][name].remove(callback_id)

    def _unload_plugin(self, plugin):
        instance = self.plugins[plugin]['instance']

        if hasattr(instance, 'close') and inspect.ismethod(instance.close):
            arglen = len(inspect.getargspec(instance.close).args)

            if arglen == 1:
                instance.close()
            elif arglen == 2:
                instance.close(self.bot)
            else:
                raise PluginError

    def _get_plugin_commands(self, instance):
        commands = []

        for _, method in inspect.getmembers(instance, predicate=inspect.ismethod):
            if callable(method) and (hasattr(method, 'commands') or hasattr(method, 'regex')):
                commands.append(method)

        return commands

    def _get_plugin_callbacks(self, instance):
        callbacks = []

        for _, method in inspect.getmembers(instance, predicate=inspect.ismethod):
            if callable(method) and hasattr(method, 'events'):
                callbacks.append(method)

        return callbacks

    def itercommands(self, channel=None):
        for name, plugin in self.plugins.items():
            if channel is not None and channel in plugin['blacklist']:
                continue

            for command in plugin['commands']:
                yield command

    def load(self, *plugins):
        failed_plugins = []

        for plugin in plugins:
            reload_flag = False

            try:
                if plugin in self.plugins.keys():
                    reload_flag = True

                    try:
                        self._unload_plugin(plugin)
                    except Exception:
                        pass

                    name = self.plugins[plugin]['module']
                else:
                    name = plugin

                module = self._load_module(name)
            except Exception:
                log.err()
                failed_plugins.append(plugin)

                continue

            try:
                instance = self._load_plugin(module)
            except Exception:
                log.err()
                failed_plugins.append(plugin)

                continue

            commands = self._get_plugin_commands(instance)
            callbacks = self._get_plugin_callbacks(instance)

            try:
                callback_ids = self._register_plugin_callbacks(callbacks)
            except Exception:
                log.err()
                failed_plugins.append(plugin)

            if plugin in self.plugins:
                self.unload(plugin)

            self.plugins[plugin] = {
                'name': plugin,
                'module': module,
                'instance': instance,
                'commands': commands,
                'callbacks': callbacks,
                'callback_ids': callback_ids,
                'blacklist': []
            }

            if reload_flag:
                self.bot.reloads += 1

        return failed_plugins

    def unload(self, *plugins):
        failed_plugins = []

        for plugin in plugins:
            if plugin not in self.plugins:
                log.err()
                failed_plugins.append(plugin)
                
                continue
            
            try:
                self._unregister_plugin_callbacks(plugin)
            except Exception:
                pass

            try:
                self._unload_plugin(plugin)
            except Exception:
                log.err()
                failed_plugins.append(plugin)

            del self.plugins[plugin]

        return failed_plugins

    def blacklist(self, plugin, *channels):
        if plugin not in self.plugins:
            return False

        self.plugins[plugin]['blacklist'].extend(channels)

        return True

    def unblacklist(self, plugin, *channels):
        if plugin not in self.plugins:
            return False

        not_blacklisted = []

        for channel in channels:
            if channel not in self.plugins[plugin]['blacklist']:
                not_blacklisted.append(channel)
                
                continue

            self.plugins[plugin]['blacklist'].remove(channel)

        return not_blacklisted

    @defer.inlineCallbacks
    def fire_command(self, user, channel, message):
        cmd, _, params = message.partition(' ')
        result = None

        for command in self.itercommands(channel):
            if hasattr(command, 'commands') and cmd in command.commands:
                try:
                    if inspect.ismethod(command):
                        result = yield defer.ensureDeferred(command(cmd, user, channel, params))
                    else:
                        result = yield defer.ensureDeferred(command(self.bot, cmd, user, channel, params))

                    if result is Event.STOP_ALL:
                        break
                except CommandNeedsArgsError:
                    continue
                except AssertionError:
                    _deferassertion()
                except:
                    self.log.failure('Failure to execute command')

            elif hasattr(command, 'regex') and re.search(command.regex, message):
                try:
                    if inspect.ismethod(command):
                        result = yield defer.ensureDeferred(command(user, channel, message))
                    else:
                        result = yield defer.ensureDeferred(command(self.bot, user, channel, message))

                    if result is Event.STOP_ALL:
                        break
                except AssertionError:
                    _deferassertion()
                except:
                    self.log.failure('Failure to execute regex')

        defer.returnValue(result)

class EventManager:
    log = Logger()

    def __init__(self, bot):
        self.bot = bot
        self.events = defaultdict(dict)
        self.callbacks = defaultdict(dict)

    def register(self, name, arglen):
        if name in self.events:
            raise EventAlreadyExistsError

        if (type(name) not in [str, Event]) or type(arglen) is not int:
            raise TypeError

        self.events[name] = arglen
        if name not in self.callbacks:
            self.callbacks[name] = {}

    def unregister(self, name):
        if name not in self.events:
            raise EventDoesNotExistError

        del self.events[name]
        del self.callbacks[name]

    def register_callback(self, name, callback):
        if not callable(callback):
            raise EventCallbackError

        if name not in self.events:
            return self._add_callback(name, callback)

        argspec = inspect.getargspec(callback)
        arglen = len(argspec.args)
        narglen = self.events[name]

        if arglen != narglen and not argspec.varargs:
            raise EventCallbackError

        return self._add_callback(name, callback)

    def unregister_callback(self, name, callback_id):
        if name not in self.callbacks:
            return

        if callback_id not in self.callbacks[name]:
            return

        del self.callbacks[name][callback_id]

    @defer.inlineCallbacks
    def fire(self, name, *params):
        if name not in self.events:
            raise EventDoesNotExistError
        
        callbacks = self.callbacks[name]
        result = None

        for callback_id, callback in callbacks.items():
            try:
                if inspect.ismethod(callback):
                    result = yield defer.ensureDeferred(callback(name, *params))
                else:
                    result = yield defer.ensureDeferred(callback(self.bot, name, *params))

                if result in [Event.STOP, Event.STOP_ALL]:
                    break
            except EventRejectedMessage:
                continue
            except AssertionError:
                _deferassertion()
            except:
                self.log.failure('Failure to execute callback')

        defer.returnValue(result)

    def _add_callback(self, name, callback):
        callback_id = EventManager._generate_id()

        while name in self.callbacks and callback_id in self.callbacks[name]:
            callback_id = EventManager._generate_id()

        self.callbacks[name][callback_id] = callback

        return callback_id

    @staticmethod
    def _generate_id(size=6, chars=string.ascii_letters + string.digits):
        return ''.join(random.choice(chars) for _ in range(size))

class Plugin:
    def __init__(self, bot):
        self.bot = bot

@unique
class Event(Enum):
    RAW = 0
    INVITE = 1
    PRIVMSG = 2
    NOTICE = 3
    NICK = 4
    MODE = 5
    TOPIC = 6
    JOIN = 7
    PART = 8
    KICK = 9
    QUIT = 10
    
    STOP = 11
    STOP_ALL = 12

def _deferassertion():
    # Twisted doesn't like it when Deferred instances are created in different threads.
    # However, there's not much we can do if we want to allow support for asyncio.
    # Sometimes you gotta play with fire and risk the burn.
    _, _, err = sys.exc_info()
    
    if traceback.extract_tb(err)[-1][0].endswith('defer.py'):
        return
    else:
        raise err

async def _resolvefunc(func, *args, **kwargs):
    if iscoroutinefunction(func):
        # async def function
        result = await func(*args, **kwargs)
    else:
        # normal function or deferred
        result = func(*args, **kwargs)

    if iscoroutine(result):
        # normal function might have called an async function
        # and didn't need the output, usually an alias
        result = await result

    return result

def event(*triggers):
    for trigger in triggers:
        if type(trigger) not in [Event, str]:
            raise TypeError

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await _resolvefunc(func, *args, **kwargs)

        wrapper.events = triggers

        return wrapper

    return decorator

def command(*triggers, **cmdkwargs):
    for trigger in triggers:
        if type(trigger) is not str:
            raise TypeError

    if 'needs_arg' in cmdkwargs.keys() and type(cmdkwargs['needs_arg']) is not bool:
        raise TypeError

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if 'needs_arg' in cmdkwargs.keys() and cmdkwargs['needs_arg'] and not len(args[-1]):
                raise CommandNeedsArgsError

            return await _resolvefunc(func, *args, **kwargs)

        wrapper.commands = triggers
        
        return wrapper
    
    return decorator

def regex(pattern):
    if type(pattern) not in [str, re._pattern_type]:
        raise TypeError

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await _resolvefunc(func, *args, **kwargs)

        wrapper.regex = pattern
        
        return wrapper

    return decorator
