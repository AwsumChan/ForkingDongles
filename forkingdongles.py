#!/usr/bin/env python3

import argparse
import sys

from twisted.application.internet import ClientService
from twisted.application.service import Application
from twisted.enterprise.adbapi import ConnectionPool

# asyncio shim
from twisted.internet import asyncioreactor
asyncioreactor.install()

from twisted.internet import endpoints, protocol, reactor
from twisted.python import log

from forkingdongles.core import ForkingDongles, ForkingDonglesFactory
from forkingdongles.utils import JSONConfig

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='A pluggable IRC bot.')
    parser.add_argument('-c', '--config', help='Configuration file', default='config.json')
    parser.add_argument('-d', '--database', help='Database file', default='forkingdongles.db')

    args = parser.parse_args()
    app = Application('ForkingDongles')
    config = JSONConfig(args.config, default={
        'core': {
            'host': 'irc.example.com',
            'port': 6667,
            'ssl': False,
            'nickname': 'ForkingDongles',
            'channels': []
        }
    })
    db = ConnectionPool('sqlite3', args.database)
    log.startLogging(sys.stderr)
    uri = '{}:{}:{}'.format(
        'ssl' if config['core']['ssl'] else 'tcp',
        config['core']['host'],
        config['core']['port'])
    endpoint = endpoints.clientFromString(reactor, uri)
    factory = ForkingDonglesFactory(config, db)
    service = ClientService(endpoint, factory)
    service.setServiceParent(app)
    service.startService()
    reactor.run()
