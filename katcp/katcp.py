
"""Utilities for dealing with KAT device control
   language messages.

   @namespace za.ac.ska.katcp
   @author Simon Cross <simon.cross@ska.ac.za>
   """

import socket
import errno
import select
import threading
import traceback
import logging
import sys
import re
import time
from sampling import SampleReactor, SampleStrategy, SampleNone
from kattypes import Int, Float, Bool, Discrete, Lru, Str, Timestamp

# logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("katcp")


class Message(object):
    """Represents a KAT device control language message."""

    # Message types
    REQUEST, REPLY, INFORM = range(3)

    # Reply codes
    # TODO: make use of reply codes in device client and server
    OK, FAIL, INVALID = "ok", "fail", "invalid"

    ## @brief Mapping from message type to string name for the type.
    TYPE_NAMES = {
        REQUEST: "REQUEST",
        REPLY: "REPLY",
        INFORM: "INFORM",
    }

    ## @brief Mapping from message type to type code character.
    TYPE_SYMBOLS = {
        REQUEST: "?",
        REPLY: "!",
        INFORM: "#",
    }

    # pylint fails to realise TYPE_SYMBOLS is defined
    # pylint: disable-msg = E0602

    ## @brief Mapping from type code character to message type.
    TYPE_SYMBOL_LOOKUP = dict((v, k) for k, v in TYPE_SYMBOLS.items())

    # pylint: enable-msg = E0602

    ## @brief Mapping from escape character to corresponding unescaped string.
    ESCAPE_LOOKUP = {
        "\\" : "\\",
        "_": " ",
        "0": "\0",
        "n": "\n",
        "r": "\r",
        "e": "\x1b",
        "t": "\t",
        "@": "",
    }

    # pylint fails to realise ESCAPE_LOOKUP is defined
    # pylint: disable-msg = E0602

    ## @brief Mapping from unescaped string to corresponding escape character.
    REVERSE_ESCAPE_LOOKUP = dict((v, k) for k, v in ESCAPE_LOOKUP.items())

    # pylint: enable-msg = E0602

    ## @brief Regular expression matching all unescaped character.
    ESCAPE_RE = re.compile(r"[\\ \0\n\r\x1b\t]")

    ## @var mtype
    # @brief Message type.

    ## @var name
    # @brief Message name.

    ## @var arguments
    # @brief List of string message arguments.

    def __init__(self, mtype, name, arguments=None):
        """Create a KATCP Message.

           @param self This object.
           @param mtype Message::Type constant.
           @param name String: message name.
           @param arguments List of strings: message arguments.
           """
        self.mtype = mtype
        self.name = name
        if arguments is None:
            self.arguments = []
        else:
            self.arguments = [str(arg) for arg in arguments]

        # check message type

        if mtype not in self.TYPE_SYMBOLS:
            raise KatcpSyntaxError("Invalid command type %r." % (mtype,))

        # check command name validity

        if not name:
            raise KatcpSyntaxError("Command missing command name.")
        if not name.replace("-","").isalnum():
            raise KatcpSyntaxError("Command name should consist only of"
                                " alphanumeric characters and dashes (got %r)."
                                % (name,))
        if not name[0].isalpha():
            raise KatcpSyntaxError("Command name should start with an"
                                " alphabetic character (got %r)."
                                % (name,))

    def copy(self):
        """Return a shallow copy of the message object and its arguments."""
        return Message(self.mtype, self.name, self.arguments)

    def __str__(self):
        """Return Message serialized for transmission.

           @param self This object.
           @return Message encoded as a ASCII string.
           """
        if self.arguments:
            escaped_args = [self.ESCAPE_RE.sub(self._escape_match, x)
                            for x in self.arguments]
            escaped_args = [x or "\\@" for x in escaped_args]
            arg_str = " " + " ".join(escaped_args)
        else:
            arg_str = ""

        return "%s%s%s" % (self.TYPE_SYMBOLS[self.mtype], self.name, arg_str)

    def _escape_match(self, match):
        """Given a re.Match object, return the escape code for it."""
        return "\\" + self.REVERSE_ESCAPE_LOOKUP[match.group()]


    # * and ** magic useful here
    # pylint: disable-msg = W0142

    @classmethod
    def request(cls, name, *args):
        """Helper method for creating request messages."""
        return cls(cls.REQUEST, name, args)

    @classmethod
    def reply(cls, name, *args):
        """Helper method for creating reply messages."""
        return cls(cls.REPLY, name, args)

    @classmethod
    def inform(cls, name, *args):
        """Helper method for creating inform messages."""
        return cls(cls.INFORM, name, args)

    # pylint: enable-msg = W0142


class KatcpSyntaxError(ValueError):
    """Exception raised by parsers on encountering syntax errors."""
    pass


class MessageParser(object):
    """Parses lines into Message objects."""

    # We only want one public method
    # pylint: disable-msg = R0903

    ## @brief Copy of TYPE_SYMBOL_LOOKUP from Message.
    TYPE_SYMBOL_LOOKUP = Message.TYPE_SYMBOL_LOOKUP

    ## @brief Copy of ESCAPE_LOOKUP from Message.
    ESCAPE_LOOKUP = Message.ESCAPE_LOOKUP

    ## @brief Regular expression matching all special characters.
    SPECIAL_RE = re.compile(r"[\0\n\r\x1b\t ]")

    ## @brief Regular expression matching all escapes.
    UNESCAPE_RE = re.compile(r"\\(.?)")

    ## @brief Regular expresion matching KATCP whitespace (just space and tab)
    WHITESPACE_RE = re.compile(r"[ \t]+")

    def _unescape_match(self, match):
        """Given an re.Match, unescape the escape code it represents."""
        char = match.group(1)
        if char in self.ESCAPE_LOOKUP:
            return self.ESCAPE_LOOKUP[char]
        elif not char:
            raise KatcpSyntaxError("Escape slash at end of argument.")
        else:
            raise KatcpSyntaxError("Invalid escape character %r." % (char,))

    def _parse_arg(self, arg):
        """Parse an argument."""
        match = self.SPECIAL_RE.search(arg)
        if match:
            raise KatcpSyntaxError("Unescaped special %r." % (match.group(),))
        return self.UNESCAPE_RE.sub(self._unescape_match, arg)

    def parse(self, line):
        """Parse a line, return a Message.

           @param self This object.
           @param line a string to parse.
           @return the resulting Message.
           """
        # find command type and check validity
        if not line:
            raise KatcpSyntaxError("Empty message received.")

        type_char = line[0]
        if type_char not in self.TYPE_SYMBOL_LOOKUP:
            raise KatcpSyntaxError("Bad type character %r." % (type_char,))

        mtype = self.TYPE_SYMBOL_LOOKUP[type_char]

        # find command and arguments name
        # (removing possible empty argument resulting from whitespace at end of command)
        parts = self.WHITESPACE_RE.split(line)
        if not parts[-1]:
            del parts[-1]

        name = parts[0][1:]
        arguments = [self._parse_arg(x) for x in parts[1:]]

        return Message(mtype, name, arguments)


class DeviceMetaclass(type):
    """Metaclass for DeviceServer and DeviceClient classes.

       Collects up methods named request_* and adds
       them to a dictionary of supported methods on the class.
       All request_* methods must have a doc string so that help
       can be generated.  The same is done for inform_* and
       reply_* methods.
       """
    def __init__(mcs, name, bases, dct):
        """Constructor for DeviceMetaclass.  Should not be used directly.

           @param mcs The metaclass instance
           @param name The metaclass name
           @param bases List of base classes
           @param dct Class dict
        """
        super(DeviceMetaclass, mcs).__init__(name, bases, dct)
        mcs._request_handlers = {}
        mcs._inform_handlers = {}
        mcs._reply_handlers = {}
        def convert(prefix, name):
            """Convert a method name to the corresponding command name."""
            return name[len(prefix):].replace("_","-")
        for name in dir(mcs):
            if not callable(getattr(mcs, name)):
                continue
            if name.startswith("request_"):
                request_name = convert("request_", name)
                mcs._request_handlers[request_name] = getattr(mcs, name)
                assert(mcs._request_handlers[request_name].__doc__ is not None)
            elif name.startswith("inform_"):
                inform_name = convert("inform_", name)
                mcs._inform_handlers[inform_name] = getattr(mcs, name)
                assert(mcs._inform_handlers[inform_name].__doc__ is not None)
            elif name.startswith("reply_"):
                reply_name = convert("reply_", name)
                mcs._reply_handlers[reply_name] = getattr(mcs, name)
                assert(mcs._reply_handlers[reply_name].__doc__ is not None)


class KatcpDeviceError(Exception):
    """Exception raised by KATCP servers when errors occur will
       communicating with a device.  Note that socket.error can also be raised
       if low-level network exceptions occurs.

       Deprecated. Servers should not raise errors if communication with a
       client fails -- errors are simply logged instead.
       """
    pass


class FailReply(Exception):
    """A custom exception which, when thrown in a request handler,
       causes DeviceServerBase to send a fail reply with the specified
       fail message, bypassing the generic exception handling, which
       would send a fail reply with a full traceback.
       """
    pass

class AsyncReply(Exception):
    """A custom exception which, when thrown in a request handler,
       indicates to DeviceServerBase that no reply has been returned
       by the handler but that the handler has arranged for a reply
       message to be sent at a later time.
       """
    pass

class DeviceServerBase(object):
    """Base class for device servers.

       Subclasses should add .request_* methods for dealing
       with request messages. These methods each take the client
       socket and msg objects as arguments and should return the
       reply message or raise an exception as a result. In these
       methods, the client socket should only be used as an argument
       to .inform().

       Subclasses can also add .inform_* and reply_* methods to handle
       those types of messages.

       Should a subclass need to generate inform messages it should
       do so using either the .inform() or .mass_inform() methods.

       Finally, this class should probably not be subclassed directly
       but rather via subclassing DeviceServer itself which implements
       common .request_* methods.
       """

    __metaclass__ = DeviceMetaclass

    def __init__(self, host, port, tb_limit=20, logger=log):
        """Create DeviceServer object.

           @param self This object.
           @param host String: host to listen on.
           @param port Integer: port to listen on.
           @param tb_limit Integer: maximum number of stack frames to
                           send in error traceback.
           @param logger Object: Logger to log to.
        """
        self._parser = MessageParser()
        self._bindaddr = (host, port)
        self._tb_limit = tb_limit
        self._running = threading.Event()
        self._sock = None
        self._thread = None
        self._logger = logger

        # sockets and data
        self._data_lock = threading.Lock()
        self._socks = [] # list of client sockets
        self._waiting_chunks = {} # map from client sockets to partial messages
        self._sock_locks = {} # map from client sockets to socket sending locks

    def _log_msg(self, level_name, msg, name, timestamp=None):
        """Create a katcp logging inform message.

           Usually this will be called from inside a DeviceLogger object,
           but it is also used by the methods in this class when errors
           need to be reported to the client.
           """
        if timestamp is None:
            timestamp = time.time()
        return Message.inform("log",
                level_name,
                str(int(timestamp * 1000.0)), # time since epoch in ms
                name,
                msg,
        )

    def bind(self, bindaddr):
        """Create a listening server socket."""
        # could be a function but we don't want it to be
        # pylint: disable-msg = R0201
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(0)
        sock.bind(bindaddr)
        sock.listen(5)
        return sock

    def add_socket(self, sock):
        """Add a client socket to the socket and chunk lists."""
        self._data_lock.acquire()
        try:
            self._socks.append(sock)
            self._waiting_chunks[sock] = ""
            self._sock_locks[sock] = threading.Lock()
        finally:
            self._data_lock.release()

    def remove_socket(self, sock):
        """Remove a client socket from the socket and chunk lists."""
        sock.close()
        self._data_lock.acquire()
        try:
            if sock in self._socks:
                self._socks.remove(sock)
                del self._waiting_chunks[sock]
                del self._sock_locks[sock]
        finally:
            self._data_lock.release()

    def get_sockets(self):
        """Return the complete list of current client socket."""
        return list(self._socks)

    def handle_chunk(self, sock, chunk):
        """Handle a chunk of data for socket sock."""
        chunk = chunk.replace("\r", "\n")
        lines = chunk.split("\n")

        waiting_chunk = self._waiting_chunks.get(sock, "")

        for line in lines[:-1]:
            full_line = waiting_chunk + line
            waiting_chunk = ""

            if full_line:
                try:
                    msg = self._parser.parse(full_line)
                # We do want to catch everything that inherits from Exception
                # pylint: disable-msg = W0703
                except Exception:
                    e_type, e_value, trace = sys.exc_info()
                    reason = "\n".join(traceback.format_exception(
                        e_type, e_value, trace, self._tb_limit
                    ))
                    self._logger.error("BAD COMMAND: %s" % (reason,))
                    self.inform(sock, self._log_msg("error", reason, "root"))
                else:
                    self.handle_message(sock, msg)

        self._data_lock.acquire()
        try:
            if sock in self._waiting_chunks:
                self._waiting_chunks[sock] = waiting_chunk + lines[-1]
        finally:
            self._data_lock.release()

    def handle_message(self, sock, msg):
        """Handle messages of all types from clients."""
        # log messages received so that no one else has to
        self._logger.debug(msg)

        if msg.mtype == msg.REQUEST:
            self.handle_request(sock, msg)
        elif msg.mtype == msg.INFORM:
            self.handle_inform(sock, msg)
        elif msg.mtype == msg.REPLY:
            self.handle_reply(sock, msg)
        else:
            reason = "Unexpected message type received by server ['%s']." \
                     % (msg,)
            self.inform(sock, self._log_msg("error", reason, "root"))

    def handle_request(self, sock, msg):
        """Dispatch a request message to the appropriate method."""
        send_reply = True
        if msg.name in self._request_handlers:
            try:
                reply = self._request_handlers[msg.name](self, sock, msg)
                assert (reply.mtype == Message.REPLY)
                assert (reply.name == msg.name)
                self._logger.info("%s OK" % (msg.name,))
            except AsyncReply, e:
                self._logger.info("%s ASYNC OK" % (msg.name,))
                send_reply = False
            except FailReply, e:
                reason = str(e)
                self._logger.error("Request %s FAIL: %s" % (msg.name, reason))
                reply = Message.reply(msg.name, "fail", reason)
            # We do want to catch everything that inherits from Exception
            # pylint: disable-msg = W0703
            except Exception:
                e_type, e_value, trace = sys.exc_info()
                reason = "\n".join(traceback.format_exception(
                    e_type, e_value, trace, self._tb_limit
                ))
                self._logger.error("Request %s FAIL: %s" % (msg.name, reason))
                reply = Message.reply(msg.name, "fail", reason)
        else:
            self._logger.error("%s INVALID: Unknown request." % (msg.name,))
            reply = Message.reply(msg.name, "invalid", "Unknown request.")

        if send_reply:
            self.send_message(sock, reply)

    def handle_inform(self, sock, msg):
        """Dispatch an inform message to the appropriate method."""
        if msg.name in self._inform_handlers:
            try:
                self._inform_handlers[msg.name](self, sock, msg)
            except Exception:
                e_type, e_value, trace = sys.exc_info()
                reason = "\n".join(traceback.format_exception(
                    e_type, e_value, trace, self._tb_limit
                ))
                self._logger.error("Inform %s FAIL: %s" % (msg.name, reason))
        else:
            self._logger.warn("%s INVALID: Unknown inform." % (msg.name,))

    def handle_reply(self, sock, msg):
        """Dispatch a reply message to the appropriate method."""
        if msg.name in self._reply_handlers:
            try:
                self._reply_handlers[msg.name](self, sock, msg)
            except Exception:
                e_type, e_value, trace = sys.exc_info()
                reason = "\n".join(traceback.format_exception(
                    e_type, e_value, trace, self._tb_limit
                ))
                self._logger.error("Reply %s FAIL: %s" % (msg.name, reason))
        else:
            self._logger.warn("%s INVALID: Unknown reply." % (msg.name,))

    def send_message(self, sock, msg):
        """Send an arbitrary message to a particular client.

           Note that failed sends disconnect the client sock and call
           on_client_disconnect. They do not raise exceptions.
           """
        # TODO: should probably implement this as a queue of sockets and messages to send.
        #       and have the queue processed in the main loop
        data = str(msg) + "\n"
        datalen = len(data)
        totalsent = 0

        # Log all sent messages here so no one else has to.
        self._logger.debug(data)

        # sends are locked per-socket -- i.e. only one send per socket at a time
        lock = self._sock_locks.get(sock)
        if lock is None:
            try:
                client_name = sock.getpeername()
            except socket.error:
                client_name = "<disconnected client>"
            msg = "Attempt to send to a socket %s which is no longer a client." % (client_name,)
            self._logger.warn(msg)
            return

        # do not do anything inside here which could call send_message!
        send_failed = False
        lock.acquire()
        try:
            while totalsent < datalen:
                try:
                    sent = sock.send(data[totalsent:])
                except socket.error, e:
                    if len(e.args) == 2 and e.args[0] == errno.EAGAIN:
                        continue
                    else:
                        send_failed = True
                        break

                if sent == 0:
                    send_failed = True
                    break

                totalsent += sent
        finally:
            lock.release()

        if send_failed:
            try:
                client_name = sock.getpeername()
            except socket.error:
                client_name = "<disconnected client>"
            msg = "Failed to send message to client %s (%s)" % (client_name, e)
            self._logger.error(msg)
            self.remove_socket(sock)
            self.on_client_disconnect(sock, msg, False)

    def inform(self, sock, msg):
        """Send an inform messages to a particular client."""
        # could be a function but we don't want it to be
        # pylint: disable-msg = R0201
        assert (msg.mtype == Message.INFORM)
        self.send_message(sock, msg)

    def mass_inform(self, msg):
        """Send an inform message to all clients."""
        assert (msg.mtype == Message.INFORM)
        for sock in list(self._socks):
            if sock is self._sock:
                continue
            self.inform(sock, msg)

    def run(self):
        """Listen for clients and process their requests."""
        timeout = 0.5 # s

        # save globals so that the thread can run cleanly
        # even while Python is setting module globals to
        # None.
        _select = select.select
        _socket_error = socket.error

        self._sock = self.bind(self._bindaddr)
        # replace bindaddr with real address so we can rebind
        # to the same port.
        self._bindaddr = self._sock.getsockname()

        self._running.set()
        while self._running.isSet():
            all_socks = self._socks + [self._sock]
            try:
                readers, _writers, errors = _select(
                    all_socks, [], all_socks, timeout
                )
            except _socket_error, e:
                # search for broken socket
                for sock in list(self._socks):
                    try:
                        _readers, _writers, _errors = _select([sock], [], [], 0)
                    except _socket_error, e:
                        self.remove_socket(sock)
                        self.on_client_disconnect(sock, "Client socket died with error %s" % (e,), False)
                # check server socket
                try:
                    _readers, _writers, _errors = _select([self._sock], [], [], 0)
                except:
                    self._logger.warn("Server socket died, attempting to restart it.")
                    self._sock = self.bind(self._bindaddr)
                # try select again
                continue

            for sock in errors:
                if sock is self._sock:
                    # server socket died, attempt restart
                    self._sock = self.bind(self._bindaddr)
                else:
                    # client socket died, remove it
                    self.remove_socket(sock)
                    self.on_client_disconnect(sock, "Client socket died", False)

            for sock in readers:
                if sock is self._sock:
                    client, addr = sock.accept()
                    client.setblocking(0)
                    self.mass_inform(Message.inform("client-connected",
                        "New client connected from %s" % (addr,)))
                    self.add_socket(client)
                    self.on_client_connect(client)
                else:
                    try:
                        chunk = sock.recv(4096)
                    except _socket_error:
                        # an error when sock was within ready list presumably
                        # means the client needs to be ditched.
                        chunk = ""
                    if chunk:
                        self.handle_chunk(sock, chunk)
                    else:
                        # no data, assume socket EOF
                        self.remove_socket(sock)
                        self.on_client_disconnect(sock, "Socket EOF", False)

        for sock in list(self._socks):
            self.on_client_disconnect(sock, "Device server shutting down.", True)
            self.remove_socket(sock)

        self._sock.close()

    def start(self, timeout=None, daemon=None):
        """Start the server in a new thread.

           @param self This object.
           @param timeout Seconds to wait for server thread to start (as a float).
           @param daemon If not None, the thread's setDaemon method is called with this
                         parameter before the thread is started. 
           @return None
           """
        if self._thread:
            raise RuntimeError("Device server already started.")

        self._thread = threading.Thread(target=self.run)
        if daemon is not None:
            self._thread.setDaemon(daemon)
        self._thread.start()
        if timeout:
            self._running.wait(timeout)
            if not self._running.isSet():
                raise RuntimeError("Device server failed to start.")

    def join(self, timeout=None):
        """Rejoin the server thread."""
        if not self._thread:
            raise RuntimeError("Device server thread not started.")

        self._thread.join(timeout)
        if not self._thread.isAlive():
            self._thread = None

    def stop(self, timeout=1.0):
        """Stop a running server (from another thread).

           @param self This object.
           @param timeout Seconds to wait for server to have *started* (as a float).
           @return None
           """
        self._running.wait(timeout)
        if not self._running.isSet():
            raise RuntimeError("Attempt to stop server that wasn't running.")
        self._running.clear()

    def running(self):
        """Whether the server is running."""
        return self._running.isSet()

    def on_client_connect(self, sock):
        """Called after client connection is established.

           Subclasses should override if they wish to send clients
           message or perform house-keeping at this point.
           """
        pass

    def on_client_disconnect(self, sock, msg, sock_valid):
        """Called before a client connection is closed.

           Subclasses should override if they wish to send clients
           message or perform house-keeping at this point. The server
           cannot guarantee this will be called (for example, the client
           might drop the connection). The message parameter contains
           the reason for the disconnection.

           @param sock Client socket being disconnected.
           @param msg Reason client is being disconnected.
           @param sock_valid True if sock is still openf for sending,
                             False otherwise.
           """
        pass


class DeviceServer(DeviceServerBase):
    """Implements some standard messages on top of DeviceServerBase.

       Inform messages handled are:
         - version (sent on connect)
         - build-state (sent on connect)
         - log (via self.log.warn(...), etc)
         - disconnect
         - client-connected

       Requests handled are:
         - halt
         - help
         - log-level
         - restart [1]
         - client-list
         - sensor-list
         - sensor-sampling
         - sensor-value
         - watchdog

       [1] Restart relies on .set_restart_queue() being used to register
           a restart queue with the device. When the device needs to be
           restarted, it will be add to the restart queue.  The queue should
           be a Python Queue.Queue object without a maximum size.
           
       Unhandled standard messages are:
         ?configure
         ?mode

       Subclasses can define the tuple VERSION_INFO to set the interface
       name, major and minor version numbers. The BUILD_INFO tuple can
       be defined to give a string describing a particular interface
       instance and may have a fourth element containing additional
       version information (e.g. rc1).

       Subclasses must override the .setup_sensors() method. If they
       have no sensors to register, the method should just be a pass.
       """

    # DeviceServer has a lot of methods because there is a method
    # per request type and it's an abstract class which is only
    # used outside this module
    # pylint: disable-msg = R0904

    ## @brief Interface version information.
    VERSION_INFO = ("device_stub", 0, 1)

    ## @brief Device server build / instance information.
    BUILD_INFO = ("name", 0, 1, "")

    ## @var log
    # @brief DeviceLogger instance for sending log messages to the client.

    # * and ** magic fine here
    # pylint: disable-msg = W0142

    def __init__(self, *args, **kwargs):
        """Create a DeviceServer."""
        super(DeviceServer, self).__init__(*args, **kwargs)
        self.log = DeviceLogger(self, python_logger=self._logger)
        self._restart_queue = None
        self._sensors = {} # map names to sensor objects
        self._reactor = None # created in run
        # map client sockets to map of sensors -> sampling strategies
        self._strategies = {}
        self.setup_sensors()

    # pylint: enable-msg = W0142

    def on_client_connect(self, sock):
        """Inform client of build state and version on connect."""
        self._strategies[sock] = {} # map of sensors -> sampling strategies
        self.inform(sock, Message.inform("version", self.version()))
        self.inform(sock, Message.inform("build-state", self.build_state()))

    def on_client_disconnect(self, sock, msg, sock_valid):
        """Inform client it is about to be disconnected."""
        if sock in self._strategies:
            strategies = self._strategies[sock]
            del self._strategies[sock]
            for sensor, strategy in list(strategies.items()):
                del strategies[sensor]
                self._reactor.remove_strategy(strategy)

        if sock_valid:
            self.inform(sock, Message.inform("disconnect", msg))

    def build_state(self):
        """Return a build state string in the form name-major.minor[(a|b|rc)n]"""
        return "%s-%s.%s%s" % self.BUILD_INFO

    def version(self):
        """Return a version string of the form type-major.minor."""
        return "%s-%s.%s" % self.VERSION_INFO

    def add_sensor(self, sensor):
        """Add a sensor to the device.

           Should only be called inside .setup_sensors().
           """
        name = sensor.name
        self._sensors[name] = sensor

    def get_sensor(self, sensor_name):
        """Fetch the sensor with the given name."""
        sensor = self._sensors.get(sensor_name, None)
        if not sensor:
            raise ValueError("Unknown sensor '%s'." % (sensor_name,))
        return sensor

    def get_sensors(self):
        """Fetch a list of all sensors"""
        return self._sensors.values()

    def set_restart_queue(self, restart_queue):
        """The the restart queue.
        
           When the device server should be restarted, it will be added to the queue.
           """
        self._restart_queue = restart_queue

    def setup_sensors(self):
        """Populate the dictionary of sensors.

           Unimplemented by default -- subclasses should add their sensors
           here or pass if there are no sensors.

           e.g. def setup_sensors(self):
                    self.add_sensor(Sensor(...))
                    self.add_sensor(Sensor(...))
                    ...
           """
        raise NotImplementedError("Device server subclasses must implement"
                                    " setup_sensors.")

    # request implementations

    # all requests take sock and msg arguments regardless of whether
    # they're used
    # pylint: disable-msg = W0613

    def request_halt(self, sock, msg):
        """Halt the device server.

        Returns
        -------
        success : {'ok', 'fail'}
            Whether scheduling the halt succeeded.

        Examples
        --------
        ?halt
        !halt ok
        """
        self.stop()
        # this message makes it through because stop
        # only registers in .run(...) after the reply
        # has been sent.
        return Message.reply("halt", "ok")

    def request_help(self, sock, msg):
        """Return help on the available requests.
        
        Return a description of the available requests using a seqeunce of #help informs.
        
        Parameters
        ----------
        request : str, optional
            The name of the request to return help for (the default is to return help for all requests).

        Inform Arguments
        ----------------
        request : str
            The name of a request.
        description : str
            Documentation for the named request.
        
        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the help succeeded.
        informs : int
            Number of #help inform messages sent.
        
        Examples
        --------
        ?help
        #help halt ...description...
        #help help ...description...
        ...
        !help ok 5

        ?help halt
        #help halt ...description...
        !help ok 1
        """
        if not msg.arguments:
            for name, method in sorted(self._request_handlers.items()):
                doc = method.__doc__
                self.inform(sock, Message.inform("help", name, doc))
            num_methods = len(self._request_handlers)
            return Message.reply("help", "ok", str(num_methods))
        else:
            name = msg.arguments[0]
            if name in self._request_handlers:
                method = self._request_handlers[name]
                doc = method.__doc__.strip()
                self.inform(sock, Message.inform("help", name, doc))
                return Message.reply("help", "ok", "1")
            return Message.reply("help", "fail", "Unknown request method.")

    def request_log_level(self, sock, msg):
        """Query or set the current logging level.
        
        Parameters
        ----------
        level : {'all', 'trace', 'debug', 'info', 'warn', 'error', 'fatal', 'off'}, optional
            Name of the logging level to set the device server to (the default is to leave the log level unchanged).

        Returns
        -------
        success : {'ok', 'fail'}
            Whether the request succeeded.
        level : {'all', 'trace', 'debug', 'info', 'warn', 'error', 'fatal', 'off'}
            The log level after processing the request.

        Examples
        --------
        ?log-level
        !log-level ok warn

        ?log-level info
        !log-level ok info
        """
        if msg.arguments:
            try:
                self.log.set_log_level_by_name(msg.arguments[0])
            except ValueError, e:
                raise FailReply(str(e))
        return Message.reply("log-level", "ok", self.log.level_name())

    def request_restart(self, sock, msg):
        """Restart the device server.

        Returns
        -------
        success : {'ok', 'fail'}
            Whether scheduling the restart succeeded.

        Examples
        --------
        ?restart
        !restart ok
        """
        if self._restart_queue is None:
            raise FailReply("No restart queue registered -- cannot restart.")
        # .put should never block because the queue should have no size limit.
        self._restart_queue.put(self)
        # this message makes it through because stop
        # only registers in .run(...) after the reply
        # has been sent.
        return Message.reply("restart", "ok")

    def request_client_list(self, sock, msg):
        """Request the list of connected clients.

        The list of clients is sent as a sequence of #client-list informs.

        Inform Arguments
        ----------------
        addr : str
            The address of the client as host:port with host in dotted quad
            notation. If the address of the client could not be determined
            (because, for example, the client disconnected suddenly) then
            a unique string representing the client is sent instead.

        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the client list succeeded.
        informs : int
            Number of #client-list inform messages sent.
        
        Examples
        --------
        ?client-list
        #client-list 127.0.0.1:53600
        !client-list ok 1
        """
        clients = self.get_sockets()
        num_clients = len(clients)
        for client in clients:
            try:
                addr = ":".join(str(part) for part in client.getpeername())
            except socket.error, e:
                # client may be gone, in which case just send a description
                addr = repr(client)
            self.inform(sock, Message.inform("client-list", addr))
        return Message.reply("client-list", "ok", str(num_clients))

    def request_sensor_list(self, sock, msg):
        """Request the list of sensors.
        
        The list of sensors is sent as a sequence of #sensor-list informs.

        Parameters
        ----------
        name : str, optional
            Name of the sensor to list (the default is to list all sensors).
        
        Inform Arguments
        ----------------
        name : str
            The name of the sensor being described.
        description : str
            Description of the named sensor.
        units : str
            Units for the value of the named sensor.
        type : str
            Type of the named sensor.
        params : list of str, optional
            Additional sensor parameters (type dependent). For integer and float
            sensors the additional parameters are the minimum and maximum sensor
            value. For discrete sensors the additional parameters are the allowed
            values. For all other types no additional parameters are sent.
            
        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the sensor list succeeded.
        informs : int
            Number of #sensor-list inform messages sent.

        Examples
        --------
        ?sensor-list
        #sensor-list psu.voltage PSU\_voltage. V float 0.0 5.0
        #sensor-list cpu.status CPU\_status. \@ discrete on off error
        ...
        !sensor-list ok 5

        ?sensor-list cpu.power.on
        #sensor-list cpu.power.on Whether\_CPU\_hase\_power. \@ boolean
        !sensor-list ok 1
        """
        if not msg.arguments:
            for name, sensor in sorted(self._sensors.iteritems(), key=lambda x: x[0]):
                self.inform(sock, Message.inform("sensor-list",
                    name, sensor.description, sensor.units, sensor.stype,
                    *sensor.formatted_params))
            return Message.reply("sensor-list",
                    "ok", str(len(self._sensors)))
        else:
            name = msg.arguments[0]
            if name in self._sensors:
                sensor = self._sensors[name]
                self.inform(sock, Message.inform("sensor-list",
                    name, sensor.description, sensor.units, sensor.stype,
                    *sensor.formatted_params))
                return Message.reply("sensor-list", "ok", "1")
            else:
                return Message.reply("sensor-list", "fail",
                                                    "Unknown sensor name.")

    def request_sensor_value(self, sock, msg):
        """Request the value of a sensor or sensors.
        
        A list of sensor values as a sequence of #sensor-value informs.
        
        Parameters
        ----------
        name : str, optional
            Name of the sensor to poll (the default is to send values for all sensors).
        
        Inform Arguments
        ----------------
        timestamp : float
            Timestamp of the sensor reading in milliseconds since the Unix epoch.
        count : {1}
            Number of sensors described in this #sensor-value inform. Will always
            be one. It exists to keep this inform compatible with #sensor-status.
        name : str
            Name of the sensor whose value is being reported.
        value : object
            Value of the named sensor. Type depends on the type of the sensor.
        
        Returns
        -------
        success : {'ok', 'fail'}
            Whether sending the list of values succeeded.
        informs : int
            Number of #sensor-value inform messages sent.
        
        Examples
        --------
        ?sensor-value
        #sensor-value 1244631611415.231 1 psu.voltage 4.5
        #sensor-value 1244631611415.200 1 cpu.status off
        ...
        !sensor-value ok 5
        
        ?sensor-value cpu.power.on
        #sensor-value 1244631611415.231 1 cpu.power.on 0
        !sensor-value ok 1
        """
        if not msg.arguments:
            for name, sensor in sorted(self._sensors.iteritems(), key=lambda x: x[0]):
                timestamp_ms, status, value = sensor.read_formatted()
                self.inform(sock, Message.inform("sensor-value",
                    timestamp_ms, "1", name, status, value))
            return Message.reply("sensor-value",
                    "ok", str(len(self._sensors)))
        else:
            name = msg.arguments[0]
            if name in self._sensors:
                sensor = self._sensors[name]
                timestamp_ms, status, value = sensor.read_formatted()
                self.inform(sock, Message.inform("sensor-value",
                    timestamp_ms, "1", name, status, value))
                return Message.reply("sensor-value", "ok", "1")
            else:
                return Message.reply("sensor-value", "fail",
                                                    "Unknown sensor name.")

    def request_sensor_sampling(self, sock, msg):
        """Configure or query the way a sensor is sampled.

        Sampled values are reported asynchronously using the #sensor-status
        message.

        Parameters
        ----------
        name : str
            Name of the sensor whose sampling strategy to query or configure.
        strategy : {'none', 'auto', 'event', 'differential', 'period'}, optional
            Type of strategy to use to report the sensor value. The differential
            strategy type may only be used with integer or float sensors.
        params : list of str, optional
            Additional strategy parameters (dependent on the strategy type).
            For the differential strategy, the parameter is an integer or float
            giving the amount by which the sensor value may change before an
            updated value is sent. For the period strategy, the parameter is the
            period to sample at in milliseconds. 

        Returns
        -------
        success : {'ok', 'fail'}
            Whether the sensor-sampling request succeeded.
        name : str
            Name of the sensor queried or configured.
        strategy : {'none', 'auto', 'event', 'differential', 'period'}
            Name of the new or current sampling strategy for the sensor.
        params : list of str
            Additional strategy parameters (see description under Parameters).
        
        Examples
        --------
        ?sensor-sampling cpu.power.on
        !sensor-sampling ok cpu.power.on none
        
        ?sensor-sampling cpu.power.on period 500
        !sensor-sampling ok cpu.power.on period 500
        """
        if not msg.arguments:
            raise FailReply("No sensor name given.")

        name = msg.arguments[0]

        if name not in self._sensors:
            raise FailReply("Unknown sensor name.")

        sensor = self._sensors[name]

        if len(msg.arguments) > 1:
            # attempt to set sampling strategy
            strategy = msg.arguments[1]
            params = msg.arguments[2:]

            if strategy not in SampleStrategy.SAMPLING_LOOKUP_REV:
                raise FailReply("Unknown strategy name.")

            def inform_callback(cb_msg):
                """Inform callback for sensor strategy."""
                self.inform(sock, cb_msg)

            new_strategy = SampleStrategy.get_strategy(strategy,
                                        inform_callback, sensor, *params)

            old_strategy = self._strategies[sock].get(sensor, None)
            if old_strategy is not None:
                self._reactor.remove_strategy(old_strategy)

            # todo: replace isinstance check with something better
            if isinstance(new_strategy, SampleNone):
                if sensor in self._strategies[sock]:
                    del self._strategies[sock][sensor]
            else:
                self._strategies[sock][sensor] = new_strategy
                self._reactor.add_strategy(new_strategy)

        current_strategy = self._strategies[sock].get(sensor, None)
        if not current_strategy:
            current_strategy = SampleStrategy.get_strategy("none", lambda msg: None, sensor)

        strategy, params = current_strategy.get_sampling_formatted()
        return Message.reply("sensor-sampling", "ok", name, strategy, *params)


    def request_watchdog(self, sock, msg):
        """Check that the server is still alive.
        
        Returns
        -------
            success : {'ok'}

        Examples
        --------
        ?watchdog
        !watchdog ok
        """
        # not a function, just doesn't use self
        # pylint: disable-msg = R0201
        return Message.reply("watchdog", "ok")

    # pylint: enable-msg = W0613

    def run(self):
        """Override DeviceServerBase.run() to ensure that the reactor thread is
           running at the same time.
           """
        self._reactor = SampleReactor()
        self._reactor.start()
        try:
            super(DeviceServer, self).run()
        finally:
            self._reactor.stop()
            self._reactor.join(timeout=0.5)
            self._reactor = None


class Sensor(object):
    """Base class for sensor classes."""

    # Sensor needs the instance attributes it has and
    # is an abstract class used only outside this module
    # pylint: disable-msg = R0902

    # Type names and formatters
    #
    # Formatters take the sensor object and the value to
    # be formatted as arguments. They may raise exceptions
    # if the value cannot be formatted.
    #
    # Parsers take the sensor object and the value to
    # parse as arguments
    #
    # type -> (name, formatter, parser)
    INTEGER, FLOAT, BOOLEAN, LRU, DISCRETE, STRING, TIMESTAMP = range(7)

    ## @brief Mapping from sensor type to tuple containing the type name,
    #  a kattype with functions to format and parse a value and a
    #  default value for sensors of that type.
    SENSOR_TYPES = {
        INTEGER: (Int, 0),
        FLOAT: (Float, 0.0),
        BOOLEAN: (Bool, False),
        LRU: (Lru, Lru.LRU_NOMINAL),
        DISCRETE: (Discrete, "unknown"),
        STRING: (Str, ""),
        TIMESTAMP: (Timestamp, 0.0),
    }

    # map type strings to types
    SENSOR_TYPE_LOOKUP = dict((v[0].name, k) for k, v in SENSOR_TYPES.items())

    # Sensor status constants
    UNKNOWN, NOMINAL, WARN, ERROR, FAILURE = range(5)

    ## @brief Mapping from sensor status to status name.
    STATUSES = {
        UNKNOWN: 'unknown',
        NOMINAL: 'nominal',
        WARN: 'warn',
        ERROR: 'error',
        FAILURE: 'failure',
    }

    ## @brief Mapping from status name to sensor status.
    STATUS_NAMES = dict((v, k) for k, v in STATUSES.items())

    # LRU sensor values
    LRU_NOMINAL, LRU_ERROR = Lru.LRU_NOMINAL, Lru.LRU_ERROR

    ## @brief Mapping from LRU value constant to LRU value name.
    LRU_VALUES = Lru.LRU_VALUES

    # LRU_VALUES not found by pylint
    # pylint: disable-msg = E0602

    ## @brief Mapping from LRU value name to LRU value constant.
    LRU_CONSTANTS = dict((v, k) for k, v in LRU_VALUES.items())

    # pylint: enable-msg = E0602

    ## @brief Number of milliseconds in a second.
    MILLISECOND = 1000

    ## @brief kattype Timestamp instance for encoding and decoding timestamps
    TIMESTAMP_TYPE = Timestamp()

    ## @var stype
    # @brief Sensor type constant.

    ## @var name
    # @brief Sensor name.

    ## @var description
    # @brief String describing the sensor.

    ## @var units
    # @brief String contain the units for the sensor value.

    ## @var params
    # @brief List of strings containing the additional parameters (length and interpretation
    # are specific to the sensor type)

    def __init__(self, sensor_type, name, description, units, params=None, default=None):
        """Instantiate a new sensor object.

           Subclasses will usually pass in a fixed sensor_type which should
           be one of the sensor type constants. The list params if set will
           have its values formatter by the type formatter for the given
           sensor type.
           """
        if params is None:
            params = []

        self._sensor_type = sensor_type
        self._observers = set()
        self._timestamp = time.time()
        self._status = Sensor.UNKNOWN

        typeclass, self._value = self.SENSOR_TYPES[sensor_type]

        if self._sensor_type in [Sensor.INTEGER, Sensor.FLOAT]:
            if not params[0] <= self._value <= params[1]:
                self._value = params[0]
            self._kattype = typeclass(params[0], params[1])
        elif self._sensor_type == Sensor.DISCRETE:
            self._value = params[0]
            self._kattype = typeclass(params)
        else:
            self._kattype = typeclass()

        self._formatter = self._kattype.pack
        self._parser = self._kattype.unpack
        self.stype = self._kattype.name

        self.name = name
        self.description = description
        self.units = units
        self.params = params
        self.formatted_params = [self._formatter(p, True) for p in params]

        if default is not None:
            self._value = default

    def attach(self, observer):
        """Attach an observer to this sensor. The observer must support a call
           to update(sensor)
           """
        self._observers.add(observer)

    def detach(self, observer):
        """Detach an observer from this sensor."""
        self._observers.discard(observer)

    def notify(self):
        """Notify all observers of changes to this sensor."""
        # copy list before iterating in case new observers arrive
        for o in list(self._observers):
            o.update(self)

    def parse_value(self, s_value):
        """Parse a value from a string.

           @param self This object.
           @param s_value the value of the sensor (as a string)
           @return None
           """
        return self._parser(s_value)

    def set(self, timestamp, status, value):
        """Set the current value of the sensor.

           @param self This object.
           @param timestamp standard python time double
           @param status the status of the sensor
           @param value the value of the sensor
           @return None
           """
        self._timestamp, self._status, self._value = timestamp, status, value
        self.notify()

    def set_formatted(self, raw_timestamp, raw_status, raw_value):
        """Set the current value of the sensor.

           @param self This object
           @param timestamp KATCP formatted timestamp string
           @param status KATCP formatted sensor status string
           @param value KATCP formatted sensor value
           @return None
           """
        timestamp = self.TIMESTAMP_TYPE.decode(raw_timestamp)
        status = self.STATUS_NAMES[raw_status]
        value = self.parse_value(raw_value)
        self.set(timestamp, status, value)

    def read_formatted(self):
        """Read the sensor and return a timestamp_ms, status, value tuple.

           All values are strings formatted as specified in the Sensor Type
           Formats in the katcp specification.
           """
        timestamp, status, value = self.read()
        return (self.TIMESTAMP_TYPE.encode(timestamp),
                self.STATUSES[status],
                self._formatter(value, True))

    def read(self):
        """Read the sensor and return a timestamp, status, value tuple.

           - timestamp: the timestamp in since the Unix epoch as a float.
           - status: Sensor status constant.
           - value: int, float, bool, Sensor value constant (for lru values)
               or str (for discrete values)

           Subclasses should implement this method.
           """
        return (self._timestamp, self._status, self._value)

    def set_value(self, value, status=NOMINAL, timestamp=None):
        """Check and then set the value of the sensor."""
        self._kattype.check(value)
        if timestamp is None:
            timestamp = time.time()
        self.set(timestamp, status, value)

    def value(self):
        """Read the current sensor value."""
        return self.read()[2]

    @classmethod
    def parse_type(cls, type_string):
        """Parse KATCP formatted type code into Sensor type constant."""
        if type_string in cls.SENSOR_TYPE_LOOKUP:
            return cls.SENSOR_TYPE_LOOKUP[type_string]
        else:
            raise KatcpSyntaxError("Invalid sensor type string %s" % type_string)

    @classmethod
    def parse_params(cls, sensor_type, formatted_params):
        """Parse KATCP formatted parameters into Python values."""
        typeclass, _value = cls.SENSOR_TYPES[sensor_type]
        if sensor_type == cls.DISCRETE:
            kattype = typeclass([])
        else:
            kattype = typeclass()
        return [kattype.decode(x) for x in formatted_params]

class DeviceLogger(object):
    """Object for logging messages from a DeviceServer.

       Log messages are logged at a particular level and under
       a particular name. Names use dotted notation to form
       a virtual hierarchy of loggers with the device."""

    # level values are used as indexes into the LEVELS list
    # so these to lists should be in the same order
    ALL, TRACE, DEBUG, INFO, WARN, ERROR, FATAL, OFF = range(8)

    ## @brief List of logging level names.
    LEVELS = [ "all", "trace", "debug", "info", "warn",
               "error", "fatal", "off" ]

    ## @brief Map of Python logging level to corresponding to KATCP levels
    PYTHON_LEVEL = {
        TRACE: 0,
        DEBUG: logging.DEBUG,
        INFO: logging.INFO,
        WARN: logging.WARN,
        ERROR: logging.ERROR,
        FATAL: logging.FATAL,
    }

    def __init__(self, device_server, root_logger="root", python_logger=None):
        """Create a DeviceLogger.

           @param self This object.
           @param device_server DeviceServer this logger logs for.
           @param root_logger String containing root logger name.
           """
        self._device_server = device_server
        self._python_logger = python_logger
        self._log_level = self.WARN
        self._root_logger_name = root_logger

    def level_name(self, level=None):
        """Return the name of the given level value.

           If level is None, return the name of the current level."""
        if level is None:
            level = self._log_level
        return self.LEVELS[level]

    def level_from_name(self, level_name):
        """Return the level constant for a given name.

           If the level_name is not known, raise a ValueError."""
        try:
            return self.LEVELS.index(level_name)
        except ValueError:
            raise ValueError("Unknown logging level name '%s'" % (level_name,))

    def set_log_level(self, level):
        """Set the logging level."""
        self._log_level = level

    def set_log_level_by_name(self, level_name):
        """Set the logging level using a level name."""
        self._log_level = self.level_from_name(level_name)

    def log(self, level, msg, name=None, timestamp=None):
        """Log a message and inform all clients."""
        if self._python_logger is not None:
            self._python_logger.log(self.PYTHON_LEVEL[level], msg)
        if level >= self._log_level:
            if name is None:
                name = self._root_logger_name
            self._device_server.mass_inform(
                self._device_server._log_msg(self.level_name(level), msg, name, timestamp=timestamp)
            )

    def trace(self, msg, name=None, timestamp=None):
        """Log a trace message."""
        self.log(self.TRACE, msg, name, timestamp)

    def debug(self, msg, name=None, timestamp=None):
        """Log a debug message."""
        self.log(self.DEBUG, msg, name, timestamp)

    def info(self, msg, name=None, timestamp=None):
        """Log an info message."""
        self.log(self.INFO, msg, name, timestamp)

    def warn(self, msg, name=None, timestamp=None):
        """Log an warning message."""
        self.log(self.WARN, msg, name, timestamp)

    def error(self, msg, name=None, timestamp=None):
        """Log an error message."""
        self.log(self.ERROR, msg, name, timestamp)

    def fatal(self, msg, name=None, timestamp=None):
        """Log a fatal error message."""
        self.log(self.FATAL, msg, name, timestamp)

    @staticmethod
    def log_to_python(logger, msg):
        """Log a KATCP logging message to a Python logger."""
        (level, timestamp, name, message) = tuple(msg.arguments)
        #created = float(timestamp) * 1e-6
        #msecs = int(timestamp) % 1000
        log_string = "%s %s: %s" % (timestamp, name, message)
        logger.log({"trace": 0,
                    "debug": logging.DEBUG,
                    "info": logging.INFO,
                    "warn": logging.WARN,
                    "error": logging.ERROR,
                    "fatal": logging.FATAL}[level], log_string)#, extra={"created": created})

