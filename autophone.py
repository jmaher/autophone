# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import Queue
import SocketServer
import datetime
import errno
import inspect
import logging
import math
import multiprocessing
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
import urlparse
import zipfile

try:
    import json
except ImportError:
    # for python 2.5 compatibility
    import simplejson as json

from manifestparser import TestManifest
from mozdevice.devicemanager import NetworkTools
from pulsebuildmonitor import start_pulse_monitor

import builds
import phonetest

from mailer import Mailer
from worker import PhoneWorker


class AutoPhone(object):

    class CmdTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):

        allow_reuse_address = True
        daemon_threads = True
        cmd_cb = None

    class CmdTCPHandler(SocketServer.BaseRequestHandler):
        def handle(self):
            buffer = ''
            self.request.send('Hello? Yes this is Autophone.\n')
            while True:
                try:
                    data = self.request.recv(1024)
                except socket.error, e:
                    if e.errno == errno.ECONNRESET:
                        break
                    raise e
                if not data:
                    break
                buffer += data
                while buffer:
                    line, nl, rest = buffer.partition('\n')
                    if not nl:
                        break
                    buffer = rest
                    line = line.strip()
                    if not line:
                        continue
                    if line == 'quit' or line == 'exit':
                        self.request.close()
                        return
                    response = self.server.cmd_cb(line)
                    self.request.send(response + '\n')

    def __init__(self, clear_cache, reboot_phones, test_path, cachefile,
                 ipaddr, port, logfile, loglevel, emailcfg, enable_pulse,
                 enable_unittests, override_build_dir,
                 repos, buildtypes):
        self._test_path = test_path
        self._cache = cachefile
        if ipaddr:
            self.ipaddr = ipaddr
        else:
            nt = NetworkTools()
            self.ipaddr = nt.getLanIp()
            logging.info('IP address for phone callbacks not provided; using '
                         '%s.' % self.ipaddr)
        self.port = port
        self.logfile = logfile
        self.loglevel = loglevel
        self.mailer = Mailer(emailcfg, '[autophone] ')
        self.build_cache = builds.BuildCache(repos, buildtypes,
                                             override_build_dir=override_build_dir,
                                             enable_unittests=enable_unittests)
        self._stop = False
        self._next_worker_num = 0
        self.phone_workers = {}  # indexed by mac address
        self.worker_lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self._tests = []
        logging.info('Starting autophone.')

        # queue for listening to status updates from tests
        self.worker_msg_queue = multiprocessing.Queue()

        self.read_tests()

        if not os.path.exists(self._cache):
            # If we don't have a cache you aren't restarting
            open(self._cache, 'wb')
        elif clear_cache:
            # If the clear cache option is specified, then blow it away and
            # recreate it
            os.remove(self._cache)
        else:
            # Otherwise assume cache is valid and read from it
            self.read_cache()
            if reboot_phones:
                self.reset_phones()

        self.server = None
        self.server_thread = None

        if enable_pulse:
            self.pulsemonitor = start_pulse_monitor(buildCallback=self.on_build,
                                                    trees=repos,
                                                    platforms=['android'],
                                                    buildtypes=buildtypes,
                                                    logger=logging.getLogger())
        else:
            self.pulsemonitor = None

        self.enable_unittests = enable_unittests

    @property
    def next_worker_num(self):
        n = self._next_worker_num
        self._next_worker_num += 1
        return n

    def run(self):
        self.server = self.CmdTCPServer(('0.0.0.0', self.port),
                                        self.CmdTCPHandler)
        self.server.cmd_cb = self.route_cmd
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.worker_msg_loop()

    def check_for_dead_workers(self):
        phoneids = self.phone_workers.keys()
        for phoneid in phoneids:
            if not self.phone_workers[phoneid].is_alive():
                print 'Worker %s died!' % phoneid
                logging.error('Worker %s died!' % phoneid)
                worker = self.phone_workers[phoneid]
                worker.stop()
                worker.crashes.add_crash()
                msg_subj = 'Worker for phone %s died' % \
                    worker.phone_cfg['phoneid']
                msg_body = 'Hello, this is Autophone. Just to let you know, ' \
                    'the worker process\nfor phone %s died.\n' % \
                    worker.phone_cfg['phoneid']
                if worker.crashes.too_many_crashes():
                    initial_state = phonetest.PhoneTestMessage.DISABLED
                    msg_subj += ' and was disabled'
                    msg_body += 'It looks really crashy, so I disabled it. ' \
                        'Sorry about that.\n'
                else:
                    initial_state = phonetest.PhoneTestMessage.DISCONNECTED
                worker.start(initial_state)
                logging.info('Sending notification...')
                try:
                    self.mailer.send(msg_subj, msg_body)
                    logging.info('Sent.')
                except socket.error:
                    logging.error('Failed to send dead-phone notification.')
                    logging.info(traceback.format_exc())

    def worker_msg_loop(self):
        # FIXME: look up worker by msg.phoneid and have worker process
        # message. worker, as part of the main process, can log status
        # and store it for later querying.
        # also, store first instance of current status (e.g. idle for 30
        # minutes, last update 1 minute ago). store test name and start time
        # if status is WORKING. All this will help us determine if and where
        # a phone/worker process is stuck.
        try:
            while not self._stop:
                self.check_for_dead_workers()
                try:
                    msg = self.worker_msg_queue.get(timeout=5)
                except Queue.Empty:
                    continue
                except IOError, e:
                    if e.errno == errno.EINTR:
                        continue
                self.phone_workers[msg.phoneid].process_msg(msg)
        except KeyboardInterrupt:
            self.stop()

    # Start the phones for testing
    def start_tests(self, job):
        if not self.is_valid_job(job):
            return
        self.worker_lock.acquire()
        for p in self.phone_workers.values():
            logging.info('Starting job on phone: %s' % p.phone_cfg['phoneid'])
            p.add_job(job)
        self.worker_lock.release()

    def route_cmd(self, data):
        # There is not currently any way to get proper responses for commands
        # that interact with workers, since communication between the main
        # process and the worker processes is asynchronous.
        # It would be possible but nontrivial for the workers to put responses
        # onto a queue and have them routed to the proper connection by using
        # request IDs or something like that.
        self.cmd_lock.acquire()
        data = data.strip()
        cmd, space, params = data.partition(' ')
        cmd = cmd.lower()
        response = 'ok'

        if cmd == 'stop':
            self.stop()
        elif cmd == 'log':
            logging.info(params)
        elif cmd == 'triggerjobs':
            self.trigger_jobs(params)
        elif cmd == 'register':
            self.register_cmd(params)
        elif cmd == 'status':
            response = ''
            now = datetime.datetime.now().replace(microsecond=0)
            for i, w in self.phone_workers.iteritems():
                response += 'phone %s (%s):\n' % (i, w.phone_cfg['ip'])
                response += '  debug level %d\n' % w.phone_cfg.get('debug', 3)
                if not w.last_status_msg:
                    response += '  no updates\n'
                else:
                    if w.last_status_msg.current_build:
                        response += '  current build: %s\n' % datetime.datetime.fromtimestamp(float(w.last_status_msg.current_build))
                    else:
                        response += '  no build loaded\n'
                    response += '  last update %s ago:\n    %s\n' % (now - w.last_status_msg.timestamp, w.last_status_msg.short_desc())
                    response += '  %s for %s\n' % (w.last_status_msg.status, now - w.first_status_of_type.timestamp)
                    if w.last_status_of_previous_type:
                        response += '  previous state %s ago:\n    %s\n' % (now - w.last_status_of_previous_type.timestamp, w.last_status_of_previous_type.short_desc())
            response += 'ok'
        elif (cmd == 'disable' or cmd == 'enable' or cmd == 'debug' or
              cmd == 'ping'):
            # Commands that take a phone as a parameter
            # FIXME: need start, stop, and remove
            # Note that disable means that the device will still be pinged
            # periodically. Do we need permanently disabled/stopped?
            phoneid, space, params = params.partition(' ')
            worker = None
            for w in self.phone_workers.values():
                if (w.phone_cfg['serial'] == phoneid or
                    w.phone_cfg['phoneid'] == phoneid):
                    worker = w
                    break
            if worker:
                f = getattr(worker, cmd)
                if params:
                    f(params)
                else:
                    f()
                response = 'ok'
                self.update_phone_cache()
            else:
                response = 'error: phone not found'
        else:
            response = 'Unknown command "%s"\n' % cmd
        self.cmd_lock.release()
        return response

    def register_phone(self, phone_cfg):
        tests = [x[0](phone_cfg=phone_cfg, config_file=x[1]) for
                 x in self._tests]

        logfile_prefix = os.path.splitext(self.logfile)[0]
        worker = PhoneWorker(self.next_worker_num, self.ipaddr,
                             tests, phone_cfg, self.worker_msg_queue,
                             '%s-%s' % (logfile_prefix, phone_cfg['phoneid']),
                             self.loglevel, self.mailer)
        self.phone_workers[phone_cfg['phoneid']] = worker
        worker.start()
        logging.info('Registered phone %s.' % phone_cfg['phoneid'])

    def register_cmd(self, data):
        # Un-url encode it
        data = urlparse.parse_qs(data.lower())

        try:
            # Map MAC Address to ip and user name for phone
            # The configparser does odd things with the :'s so remove them.
            macaddr = data['name'][0].replace(':', '_')
            phoneid = '%s_%s' % (macaddr, data['hardware'][0])

            if phoneid not in self.phone_workers:
                phone_cfg = dict(
                    phoneid=phoneid,
                    serial=data['pool'][0].upper(),
                    ip=data['ipaddr'][0],
                    sutcmdport=int(data['cmdport'][0]),
                    machinetype=data['hardware'][0],
                    osver=data['os'][0],
                    debug=3,
                    ipaddr=self.ipaddr)
                self.register_phone(phone_cfg)
                self.update_phone_cache()
            else:
                logging.debug('Registering known phone: %s' %
                              self.phone_workers[phoneid].phone_cfg['phoneid'])
        except:
            print 'ERROR: could not write cache file, exiting'
            traceback.print_exception(*sys.exc_info())
            self.stop()

    def read_cache(self):
        self.phone_workers.clear()
        try:
            with open(self._cache) as f:
                try:
                    cache = json.loads(f.read())
                except ValueError:
                    cache = {}

                for phone_cfg in cache.get('phones', []):
                    self.register_phone(phone_cfg)
        except IOError, err:
            if err.errno != errno.ENOENT:
                raise err

    def update_phone_cache(self):
        cache = {}
        cache['phones'] = [x.phone_cfg for x in self.phone_workers.values()]
        with open(self._cache, 'w') as f:
            f.write(json.dumps(cache))

    def read_tests(self):
        self._tests = []
        manifest = TestManifest()
        manifest.read(self._test_path)
        tests_info = manifest.get()
        for t in tests_info:
            if not t['here'] in sys.path:
                sys.path.append(t['here'])
            if t['name'].endswith('.py'):
                t['name'] = t['name'][:-3]
            # add all classes in module that are derived from PhoneTest to
            # the test list
            tests = [(x[1], os.path.normpath(os.path.join(t['here'],
                                                          t.get('config', ''))))
                     for x in inspect.getmembers(__import__(t['name']),
                                                 inspect.isclass)
                     if x[0] != 'PhoneTest' and issubclass(x[1],
                                                           phonetest.PhoneTest)]
            self._tests.extend(tests)

    def trigger_jobs(self, data):
        logging.debug('trigger_jobs: data  %s' % data)
        job = self.build_job(self.get_build(data))
        logging.info('Received user-specified job: %s' % job)
        self.start_tests(job)

    def reset_phones(self):
        logging.info('Resetting phones...')
        for phoneid, phone in self.phone_workers.iteritems():
            phone.reboot()

    def on_build(self, msg):
        # Use the msg to get the build and install it then kick off our tests
        logging.debug('---------- BUILD FOUND ----------')
        logging.debug('%s' % msg)
        logging.debug('---------------------------------')

        # We will get a msg on busted builds with no URLs, so just ignore
        # those, and only run the ones with real URLs
        # We create jobs for all the phones and push them into the queue
        if 'buildurl' in msg:
            self.start_tests(self.build_job(self.get_build(msg['buildurl'])))

    def get_build(self, buildurl):
        cache_build_dir = self.build_cache.get(buildurl,
                                               self.enable_unittests)
        if not cache_build_dir:
            logging.warn('Errors occured getting build %s.' % buildurl)
            return None
        try:
            build_path = os.path.join(cache_build_dir, 'build.apk')
            z = zipfile.ZipFile(build_path)
            z.testzip()
        except zipfile.BadZipfile:
            logging.error('%s is a bad apk; redownloading...' % build_path)
            cache_build_dir = self.build_cache.get(buildurl,
                                                   self.enable_unittests,
                                                   force=True)
        return cache_build_dir

    def build_job(self, cache_build_dir):
        if not cache_build_dir:
            logging.warn('No build available. Aborting job.')
            return None
        tmpdir = tempfile.mkdtemp()
        try:
            build_path = os.path.join(cache_build_dir, 'build.apk')
            apkfile = zipfile.ZipFile(build_path)
            apkfile.extract('application.ini', tmpdir)
        except zipfile.BadZipfile:
            # we should have already tried to redownload bad zips, so treat
            # this as fatal.
            logging.error('%s is a bad apk; aborting job.' % build_path)
            shutil.rmtree(tmpdir)
            return None
        cfg = ConfigParser.RawConfigParser()
        cfg.read(os.path.join(tmpdir, 'application.ini'))
        rev = cfg.get('App', 'SourceStamp')
        ver = cfg.get('App', 'Version')
        repo = cfg.get('App', 'SourceRepository')
        buildid = cfg.get('App', 'BuildID')
        blddate = datetime.datetime.strptime(buildid,
                                             '%Y%m%d%H%M%S')
        procname = ''
        if repo == 'http://hg.mozilla.org/mozilla-central':
            tree = 'mozilla-central'
            procname = 'org.mozilla.fennec'
        elif repo == 'http://hg.mozilla.org/integration/mozilla-inbound':
            tree = 'mozilla-inbound'
            procname = 'org.mozilla.fennec'
        elif repo == 'http://hg.mozilla.org/releases/mozilla-aurora':
            tree = 'mozilla-aurora'
            procname = 'org.mozilla.fennec_aurora'
        elif repo == 'http://hg.mozilla.org/releases/mozilla-beta':
            tree = 'mozilla-beta'
            procname = 'org.mozilla.firefox'

        job = {'cache_build_dir': cache_build_dir,
               'tree': tree,
               'blddate': math.trunc(time.mktime(blddate.timetuple())),
               'buildid': buildid,
               'revision': rev,
               'androidprocname': procname,
               'version': ver,
               'bldtype': 'opt'}
        shutil.rmtree(tmpdir)
        return job

    def is_valid_job(self, job):
        if job is None:
            return False

        error_list = []

        if 'androidprocname' not in job:
            error_list.append('missing androidprocname')

        if 'revision' not in job:
            error_list.append('missing revision')

        if 'blddate' not in job:
            error_list.append('missing blddate')

        if 'bldtype' not in job:
            error_list.append('missing bldtype')

        if 'version' not in job:
            error_list.append('missing version')

        if len(error_list) > 0:
            error_message = 'ERROR: Invalid job configuration: %s ' % job + ', '.join(error_list)
            self.logger.error(error_message)
            raise NameError(error_message)

        return True

    def stop(self):
        self._stop = True
        self.server.shutdown()
        for p in self.phone_workers.values():
            p.stop()
        self.server_thread.join()


def main(clear_cache, reboot_phones, test_path, cachefile, ipaddr, port,
         logfile, loglevel_name, emailcfg, enable_pulse, enable_unittests,
         override_build_dir, repos, buildtypes):

    def sigterm_handler(signum, frame):
        autophone.stop()

    loglevel = e = None
    try:
        loglevel = getattr(logging, loglevel_name)
    except AttributeError, e:
        pass
    finally:
        if e or logging.getLevelName(loglevel) != loglevel_name:
            print 'Invalid log level %s' % loglevel_name
            return errno.EINVAL

    logging.basicConfig(filename=logfile,
                        filemode='a',
                        level=loglevel,
                        format='%(asctime)s|%(levelname)s|%(message)s')

    print '%s Starting server on port %d.' % \
        (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), port)
    try:
        autophone = AutoPhone(clear_cache, reboot_phones, test_path, cachefile,
                              ipaddr, port, logfile, loglevel, emailcfg,
                              enable_pulse, enable_unittests,
                              override_build_dir,
                              repos, buildtypes)
    except builds.BuildCacheException, e:
        print '''%s

When specifying --override-build-dir, the directory must already exist
and contain a build.apk package file to be tested.

In addition, if you have specified --enable-unittests, the override
build directory must also contain a tests directory containing the
unpacked tests package for the build.

        ''' % e
        parser.print_help()
        return 1

    signal.signal(signal.SIGTERM, sigterm_handler)
    autophone.run()
    print '%s AutoPhone terminated.' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return 0


if __name__ == '__main__':
    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option('--clear-cache', action='store_true', dest='clear_cache',
                      default=False,
                      help='If specified, we clear the information in the '
                      'autophone cache before starting')
    parser.add_option('--no-reboot', action='store_false', dest='reboot_phones',
                      default=True, help='Indicates that phones should not be '
                      'rebooted when autophone starts (ignored if '
                      '--clear-cache is used')
    parser.add_option('--ipaddr', action='store', type='string', dest='ipaddr',
                      default=None, help='IP address of interface to use for '
                      'phone callbacks, e.g. after rebooting. If not given, '
                      'it will be guessed.')
    parser.add_option('--port', action='store', type='int', dest='port',
                      default=28001,
                      help='Port to listen for incoming connections, defaults '
                      'to 28001')
    parser.add_option('--cache', action='store', type='string', dest='cachefile',
                      default='autophone_cache.json',
                      help='Cache file to use, defaults to autophone_cache.json '
                      'in local dir')
    parser.add_option('--logfile', action='store', type='string',
                      dest='logfile', default='autophone.log',
                      help='Log file to store logging from entire system. '
                      'Individual phone worker logs will use '
                      '<logfile>-<phoneid>[.<ext>]. Default: autophone.log')
    parser.add_option('--loglevel', action='store', type='string',
                      dest='loglevel', default='DEBUG',
                      help='Log level - ERROR, WARNING, DEBUG, or INFO, '
                      'defaults to DEBUG')
    parser.add_option('-t', '--test-path', action='store', type='string',
                      dest='test_path', default='tests/manifest.ini',
                      help='path to test manifest')
    parser.add_option('--emailcfg', action='store', type='string',
                      dest='emailcfg', default='email.ini',
                      help='config file for email settings; defaults to email.ini')
    parser.add_option('--disable-pulse', action='store_false',
                      dest="enable_pulse", default=True,
                      help="Disable connecting to pulse to look for new builds")
    parser.add_option('--enable-unittests', action='store_true',
                      dest='enable_unittests', default=False,
                      help='Enable running unittests by downloading and installing '
                      'the unittests package for each build')
    parser.add_option('--override-build-dir', type='string',
                      dest='override_build_dir', default=None,
                      help='Use the specified directory as the current build '
                      'cache directory without attempting to download a build '
                      'or test package.')
    parser.add_option('--repo',
                      dest='repos',
                      action='append',
                      help='The repos to test. '
                      'One of mozilla-central, mozilla-inbound, mozilla-aurora, '
                      'mozilla-beta. To specify multiple repos, specify them '
                      'with additional --repo options. Defaults to mozilla-central.')
    parser.add_option('--buildtype',
                      dest='buildtypes',
                      action='append',
                      help='The build types to test. '
                      'One of opt or debug. To specify multiple build types, '
                      'specify them with additional --buildtype options. '
                      'Defaults to opt.')

    (options, args) = parser.parse_args()
    if not options.repos:
        options.repos = ['mozilla-central']

    if not options.buildtypes:
        options.buildtypes = ['opt']

    exit_code = main(options.clear_cache, options.reboot_phones,
                     options.test_path, options.cachefile, options.ipaddr,
                     options.port, options.logfile, options.loglevel,
                     options.emailcfg, options.enable_pulse,
                     options.enable_unittests, options.override_build_dir,
                     options.repos, options.buildtypes)

    sys.exit(exit_code)
