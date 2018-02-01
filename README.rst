ForkingDongles
==============
A pluggable IRC bot.
--------------------
Written with the Twisted asynchronous event framework, ForkingDongles aims to be a fast, extendable, and capable
IRC bot framework.

Installation
============
To install ForkingDongles, you'll need a recent version of Python 3 (3.5 or newer) and pip for package dependencies.
Use of virtualenv is highly recommended. When you obtain the source, run this in the main directory::

  $ pip install -r requirements.txt
  ...
  $ mkdir plugins

This should install the required dependencies, as well as create the folder where you'll store your plugins.
You can start the bot currently by running the following command::

  $ ./forkingdongles.py
  
The initial run will create a new configuration file located in the same directory named "config.json".
Edit it to your liking (the default settings are non-functional) and upon running the command again with valid server
settings, everything should work fine.

Documentation
=============
Documentation for ForkingDongles is currently a work in progress and will be generated with Sphinx via docstrings.

License
=======

See LICENSE.txt for details.
