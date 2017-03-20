#
# This file is part of Gruvi. Gruvi is free software available under the
# terms of the MIT license. See the file "LICENSE" that was provided
# together with this source file for the licensing terms.
#
# Copyright (c) 2012-2017 the Gruvi authors. See the file "AUTHORS" for a
# complete list.

"""
The :mod:`gruvi.http` module implements a HTTP client and server.

The client and server are relatively complete implementations of the HTTP
protocol. Some of the supported features are: keepalive, pipelining, chunked
transfers and trailers.

Some general notes about the implementation:

* Both HTTP/1.0 and HTTP/1.1 are supported. The client will by default make
  requests with HTTP/1.1. The server always responds in the same version as the
  request.
* Connections are kept alive by default. This means that you need to make sure
  you close connections when they are no longer needed.
* Any headers that are passed in by application code must not be "Hop by hop"
  headers. These headers may only be used by HTTP implementations themselves,
  such as the client and server in this module.

Some important points about the use of binary versus unicode types in the API:

* Data that is passed into the API that ends up in the HTTP header, such as the
  HTTP version string, method, and headers, must be of the string type. This
  means ``str`` on Python 3, and ``str`` or ``unicode`` on Python 2. However,
  if the string type is unicode aware (all except ``str`` on Python 2), you
  must make sure that it only contains code points that are defined in
  ISO-8859-1, which is the default HTTP encoding specified in RFC2606.
* In theory, HTTP headers can support unicode code points outside ISO-8859-1 if
  encoded according to the scheme in RFC2047. However this method is very
  poorly supported and rarely used, and Gruvi therefore does not offer any
  special support for it. If you must use this feature for some reason, you can
  pre-encode the headers into this encoding and pass them already encoded.
* Data that is passed to the API and ends up in the HTTP body can be either of
  the binary type or of the string type (``bytes``, ``str`` or ``unicode``, the
  latter only on Python 2). If passing a unicode aware type, then the data is
  encoded before adding it to the body. The encoding must be passed into the
  client or server by passing a "Content-Type" header with a "charset"
  parameter. If no encoding is provided, then ISO-8859-1 is assumed. Note that
  ISO-8859-1 is not able to express any code points outside latin1. This means
  that if you pass a body with non-latin1 code points, and you fail to set the
  "charset" parameter, then you will get a ``UnicodeEncodeError`` exception.
"""

from __future__ import absolute_import, print_function

import re
import time
import textwrap
import functools
import six

from . import logging, compat
from .hub import switchpoint
from .util import delegate_method
from .errors import Error
from .protocols import MessageProtocol
from .stream import Stream
from .endpoints import Client, Server
from .http_ffi import lib, ffi

from six.moves import http_client
from six.moves.urllib_parse import urlsplit

__all__ = ['HttpError', 'HttpRequest', 'HttpResponse', 'HttpProtocol',
           'HttpClient', 'HttpServer']


# Export some definitions from  http.client.
for name in dir(http_client):
    value = getattr(http_client, name)
    if not name.isupper() or not isinstance(value, int):
        continue
    if value in http_client.responses:
        globals()[name] = value

HTTP_PORT = http_client.HTTP_PORT
HTTPS_PORT = http_client.HTTPS_PORT
responses = http_client.responses


# The "Hop by Hop" headers as defined in RFC 2616. These may not be set by the
# HTTP handler.
hop_by_hop = frozenset(('connection', 'keep-alive', 'proxy-authenticate',
                        'proxy-authorization', 'te', 'trailers',
                        'transfer-encoding', 'upgrade'))


# Keep a cache of HTTP methods numbers -> method strings
_http_methods = {}
for i in range(100):
    method = ffi.string(lib.http_method_str(i)).decode('ascii')
    if not method.isupper():
        break
    _http_methods[i] = method


# RFC 2626 section 2.2 grammar definitions:
_re_token = re.compile('([!#$%&\'*+-.0-9A-Z^_`a-z|~]+)')

# The regex for "quoted_string" below is not 100% correct. The standard allows
# also LWS and escaped CTL characters. But http-parser has an issue with these
# so we just not allow them.
# Note that the first 256 code points of Unicode are the same as those for
# ISO-8859-1 which is how HTTP headers are encoded. So we can just include the
# valid characters as \x hex references.
# Also note that this does not decode any of the RFC-2047 internationalized
# header values that are allowed in quoted-string (but it will match).
_re_qstring = re.compile('"(([ !\x23-\xff]|\\")*)"')


def parse_option_header(header, sep=';'):
    """Parse a HTTP header with options.

    The header must be of the form "value [; parameters]". This format is used
    by headers like "Content-Type" and "Transfer-Encoding".

    The return value is a (value, params) tuple, with params a dictionary
    containing the parameters.

    This function never raises an error. When a parse error occurs, it returns
    what has been parsed so far.
    """
    options = {}
    p1 = header.find(sep)
    if p1 == -1:
        return header, options
    p2 = p1+1
    while True:
        while p2 < len(header) and header[p2].isspace():
            p2 += 1
        if p2 == len(header):
            break
        mobj = _re_token.match(header, p2)
        if mobj is None:
            break
        name = mobj.group(1)
        p2 = mobj.end(0)
        if p2 > len(header)-2 or header[p2] != '=':
            break
        p2 += 1
        if header[p2] == '"':
            mobj = _re_qstring.match(header, p2)
        else:
            mobj = _re_token.match(header, p2)
        if mobj is None:
            break
        value = mobj.group(1)
        p2 = mobj.end(0)
        options[name] = value
    return header[:p1], options


_weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
_months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
_rfc1123_fmt = '%a, %d %b %Y %H:%M:%S GMT'
_last_stamp = None
_last_date = None

def rfc1123_date(timestamp=None):
    """Create a RFC1123 style Date header for *timestamp*.

    If *timestamp* is None, use the current time.
    """
    global _last_stamp, _last_date
    if timestamp is None:
        timestamp = int(time.time())
    if timestamp == _last_stamp:
        return _last_date
    tm = time.gmtime(timestamp)
    # The time stamp must be GMT, and cannot be localized
    s = _rfc1123_fmt.replace('%a', _weekdays[tm.tm_wday]) \
                    .replace('%b', _months[tm.tm_mon-1])
    _last_date = time.strftime(s, tm)
    _last_stamp = timestamp
    return _last_date


def _s2b(s):
    """Convert a string *s* to bytes in the ISO-8859-1 encoding.

    ISO-8859-1 is the default encoding used in HTTP.
    """
    if type(s) is not bytes:
        s = s.encode('iso-8859-1')
    return s

def _ba2s(ba):
    """Convert a byte-array to a "str" type."""
    if six.PY3:
        return ba.decode('iso-8859-1')
    else:
        return bytes(ba)

def _cd2s(cd):
    """Convert a cffi cdata('char *') to a str."""
    s = ffi.string(cd)
    if six.PY3:
        s = s.decode('iso-8859-1')
    return s


def get_field(headers, name, default=None):
    """Return a field value from a list with (name, value) tuples."""
    name = name.lower()
    for header in headers:
        if header[0].lower() == name:
            return header[1]
    return default


def create_chunk(buf):
    """Create a chunk for the HTTP "chunked" transfer encoding."""
    chunk = bytearray()
    chunk.extend(_s2b('{:X}\r\n'.format(len(buf))))
    chunk.extend(_s2b(buf))
    chunk.extend(b'\r\n')
    return chunk


def create_chunked_body_end(trailers=None):
    """Create the ending that terminates a chunked body."""
    ending = bytearray()
    ending.extend(b'0\r\n')
    if trailers:
        for name, value in trailers:
            ending.extend(_s2b('{}: {}\r\n'.format(name, value)))
    ending.extend(b'\r\n')
    return ending


def create_request(version, method, url, headers):
    """Create a HTTP request header."""
    # According to my measurements using b''.join is faster that constructing a
    # bytearray.
    message = []
    message.append(_s2b('{} {} HTTP/{}\r\n'.format(method, url, version)))
    for name, value in headers:
        message.append(_s2b('{}: {}\r\n'.format(name, value)))
    message.append(b'\r\n')
    return b''.join(message)


def create_response(version, status, headers):
    """Create a HTTP response header."""
    message = []
    message.append(_s2b('HTTP/{} {}\r\n'.format(version, status)))
    for name, value in headers:
        message.append(_s2b('{}: {}\r\n'.format(name, value)))
    message.append(b'\r\n')
    return b''.join(message)


class HttpError(Error):
    """Exception that is raised in case of HTTP protocol errors."""


class HttpMessage(object):
    """A HTTP message (request or response).

    This is an internal class used by the parser.
    """

    def __init__(self):
        self.message_type = None
        self.version = None
        self.status_code = None
        self.method = None
        self.url = None
        self.is_upgrade = None
        self.should_keep_alive = None
        self.parsed_url = None
        self.headers = []
        self.trailers = []
        self.body = None


class ErrorStream(object):
    """Passed to the WSGI application as environ['wsgi.errors'].

    Forwards messages to the Python logging facility.
    """

    __slots__ = ['_log']

    def __init__(self, log=None):
        self._log = log or logging.get_logger()

    def flush(self):
        pass

    def write(self, data):
        self._log.error('wsgi.errors: {}', data)

    def writelines(self, seq):
        for line in seq:
            self.write(line)


class HttpRequest(object):
    """A HTTP request.

    Instances of this class are returned by :meth:`HttpProtocol.request`. This
    class allows you to write the request body yourself using the :meth:`write`
    and :meth:`end_request` methods. This can be useful if you need to send a
    large input that cannot be easily presented as as a file-like object or a
    generator, or if you want to use "chunked" encoding trailers.
    """

    def __init__(self, protocol):
        self._protocol = protocol
        self._chunked = False
        self._charset = 'ISO-8859-1'
        self._content_length = None
        self._bytes_written = 0

    @switchpoint
    def start_request(self, method, url, headers=None, body=None):
        """Start a new HTTP request.

        This method is called by :meth:`HttpProtocol.request`. It creates a new
        HTTP request header and sends it to the transport.

        The *body* parameter is a hint that specifies the body that will be
        sent in the future, but it will not actually send it. This method tries
        to deduce the Content-Length of the body that follows from it.
        """
        headers = headers[:] if headers is not None else []
        agent = host = clen = ctype = None
        # Ensure that the user doesn't provide any hop-by-hop headers. Only
        # HTTP applications are allowed to set these.
        for name, value in headers:
            name = name.lower()
            if name in hop_by_hop:
                raise ValueError('header {} is hop-by-hop'.format(name))
            elif name == 'user-agent':
                agent = value
            elif name == 'host':
                host = value
            elif name == 'content-type':
                ctype, params = parse_option_header(value)
                self._charset = params.get('charset')
            elif name == 'content-length':
                clen = int(value)
        # Check that we can support the body type.
        if not isinstance(body, (six.binary_type, six.text_type)) \
                    and not hasattr(body, 'read') \
                    and not hasattr(body, '__iter__') \
                    and body is not None:
            raise TypeError('body: expecting a bytes or str instance, '
                            'a file-like object or an iterable')
        version = self._protocol._version
        # The Host header is mandatory in 1.1. Add it if it's missing.
        server_name = self._protocol.server_name
        if host is None and version == '1.1' and server_name:
            headers.append(('Host', server_name))
        # Identify ourselves.
        if agent is None:
            headers.append(('User-Agent', self._protocol.identifier))
        # Check if we know the body length. If not, then we require "chunked"
        # encoding. Then determine if we can keep the connection alive.
        if isinstance(body, six.text_type):
            body = body.encode(self._charset)
        if clen is None:
            if isinstance(body, six.binary_type):
                clen = len(body)
                if clen > 0:
                    headers.append(('Content-Length', str(clen)))
            elif version == '1.1':
                self._chunked = True
            else:
                raise ValueError('headers: must have "Content-Length" header '
                                 'for HTTP 1.0 when body size unknown')
        self._content_length = clen
        # On HTTP/1.0 we need to specifically indicate we want keep-alive.
        if version == '1.0':
            headers.append(('Connection', 'keep-alive'))
        # If we're doing chunked then we can also do trailers.
        if self._chunked:
            headers.append(('Transfer-Encoding', 'chunked'))
            headers.append(('TE', 'trailers'))
        # Start the request
        header = create_request(version, method, url, headers)
        self._protocol._writer.write(header)

    @switchpoint
    def write(self, buf):
        """Write *buf* to the request body."""
        if not buf:
            return
        if isinstance(buf, six.text_type):
            buf = buf.encode(self._charset)
        self._bytes_written += len(buf)
        if self._content_length is not None and self._bytes_written > self._content_length:
            raise RuntimeError('wrote too many bytes ({} > {})'
                                    .format(self._bytes_written, self._content_length))
        if self._chunked:
            buf = create_chunk(buf)
        self._protocol._writer.write(buf)

    @switchpoint
    def end_request(self, trailers=None):
        """End the request body.

        The optional *trailers* argument can be used to add trailers. This
        requires "chunked" encoding.
        """
        if trailers and not self._chunked:
            raise RuntimeError('trailers require "chunked" encoding')
        if self._chunked:
            ending = create_chunked_body_end(trailers)
            self._protocol._writer.write(ending)


class HttpResponse(object):
    """A HTTP response.

    Instances of this class are returned by :meth:`HttpProtocol.getresponse`.
    This class allows you to get access to all information related to a HTTP
    response, including the HTTP headers and body.
    """

    def __init__(self, message):
        self._message = message

    @property
    def version(self):
        """The HTTP version as a (major, minor) tuple."""
        return self._message.version

    @property
    def status(self):
        """The HTTP status code, as an integer."""
        return self._message.status_code

    @property
    def headers(self):
        """The response headers, as a list of (name, value) pairs."""
        return self._message.headers

    @property
    def trailers(self):
        """The response trailers, as a list of (name, value) pairs.

        The trailers will only be available after the entire response has been
        read. Most servers do not generate trailers.
        """
        return self._message.trailers

    def get_header(self, name, default=None):
        """Return the value of HTTP header *name*.

        If the header does not exist, return *default*.
        """
        return get_field(self._message.headers, name, default)

    def get_trailer(self, name, default=None):
        """Return a the value of a HTTP trailer *name*.

        If the trailer does not exist, return *default*.
        """
        return get_field(self._message.trailers, name, default)

    @property
    def body(self):
        """A :class:`gruvi.Stream` instance for reading the response body."""
        return self._message.body

    delegate_method(body, Stream.read)
    delegate_method(body, Stream.read1)
    delegate_method(body, Stream.readline)
    delegate_method(body, Stream.readlines)
    delegate_method(body, Stream.__iter__)


class WsgiHandler(object):
    """An adapter that runs a WSGI application as a :class:`MessageProtocol`
    message handler.

    This class is used internally by :class:`HttpProtocol`.
    """

    def __init__(self, application):
        """
        The *transport* and *protocol* arguments are the connection's
        transport and protocol respectively.

        The *message* argument must be a :class:`HttpMessage`.
        """
        self._application = application
        self._transport = None
        self._protocol = None
        self._message = None
        self._log = logging.get_logger()
        self._environ = {}
        self._status = None
        self._headers = None

    @switchpoint
    def send_headers(self):
        """Send the HTTP headers and start the response body."""
        # We need to figure out the transfer encoding of the body that will
        # follow the header. Here's what we do:
        #  - If there's a content length, don't use any TE.
        #  - Otherwise, if the protocol is HTTP/1.1, use "chunked".
        #  - Otherwise, close the connection after the body is sent.
        clen = get_field(self._headers, 'Content-Length')
        version = self._message.version
        self._chunked = clen is None and version == '1.1'
        if self._chunked:
            self._headers.append(('Transfer-Encoding', 'chunked'))
        # The client may also ask to close the connection (Connection: close)
        self._keepalive = self._message.should_keep_alive and (self._chunked or clen)
        # The default on HTTP/1.1 is keepalive, on HTTP/1.0 it is to close.
        if version == '1.1' and not self._keepalive:
            self._headers.append(('Connection', 'close'))
        elif version == '1.0' and self._keepalive:
            self._headers.append(('Connection', 'keep-alive'))
        server = get_field(self._headers, 'Server')
        if server is None:
            self._headers.append(('Server', self._protocol.identifier))
        date = get_field(self._headers, 'Date')
        if date is None:
            self._headers.append(('Date', rfc1123_date()))
        header = create_response(version, self._status, self._headers)
        self._protocol._writer.write(header)

    def start_response(self, status, headers, exc_info=None):
        """Callable to be passed to the WSGI application."""
        if exc_info:
            try:
                if self._headers_sent:
                    six.reraise(*exc_info)
            finally:
                exc_info = None
        elif self._status is not None:
            raise RuntimeError('response already started')
        for name, value in headers:
            if name.lower() in hop_by_hop:
                raise ValueError('header {} is hop-by-hop'.format(name))
        self._status = status
        self._headers = headers
        return self.write

    @switchpoint
    def write(self, data):
        """Callable passed to the WSGI application by :meth:`start_response`."""
        if isinstance(data, six.text_type):
            data = data.encode('iso-8859-1')
        elif not isinstance(data, six.binary_type):
            raise TypeError('data: expecting bytes or str instance')
        elif not data:
            return
        if not self._headers_sent:
            self.send_headers()
            self._headers_sent = True
        if self._chunked:
            data = create_chunk(data)
        self._protocol._writer.write(data)

    @switchpoint
    def end_response(self):
        """End a response."""
        if not self._headers_sent:
            self.send_headers()
            self._headers_sent = True
        if self._chunked:
            trailers = self._environ.get('gruvi.trailers')
            ending = create_chunked_body_end(trailers)
            self._protocol._writer.write(ending)
        if not self._message.should_keep_alive:
            self._protocol._writer.close()

    @switchpoint
    def __call__(self, message, transport, protocol):
        """Run a WSGI handler."""
        if self._transport is None:
            self._transport = transport
            self._protocol = protocol
        self._status = None
        self._headers = None
        self._headers_sent = False
        self._chunked = False
        self._message = message
        if not self._environ:
            self.create_environ()
        self.update_environ()
        if __debug__:
            self._log.debug('request: {} {}', message.method, message.url)
        result = None
        try:
            result = self._application(self._environ, self.start_response)
            if not self._status:
                raise HttpError('WSGI handler did not call start_response()')
            for chunk in result:
                self.write(chunk)
            self.end_response()
        finally:
            if hasattr(result, 'close'):
                result.close()
        if __debug__:
            ctype = get_field(self._headers, 'Content-Type', 'unknown')
            clen = get_field(self._headers, 'Content-Length', 'unknown')
            self._log.debug('response: {} ({}; {} bytes)'.format(self._status, ctype, clen))

    def create_environ(self):
        # Initialize the environment with per connection variables.
        env = self._environ
        # CGI variables
        env['SCRIPT_NAME'] = ''
        sockname = self._transport.get_extra_info('sockname')
        if isinstance(sockname, tuple):
            env['SERVER_NAME'] = self._protocol.server_name or sockname[0]
            env['SERVER_PORT'] = str(sockname[1])
        else:
            env['SERVER_NAME'] = self._protocol.server_name or sockname
            env['SERVER_PORT'] = ''
        env['SERVER_SOFTWARE'] = self._protocol.identifier
        # SSL information
        sslsock = self._transport.get_extra_info('sslsocket')
        cipherinfo = sslsock.cipher() if sslsock else None
        if sslsock and cipherinfo:
            env['HTTPS'] = '1'
            env['SSL_CIPHER'] = cipherinfo[0]
            env['SSL_PROTOCOL'] = cipherinfo[1]
            env['SSL_CIPHER_USEKEYSIZE'] = int(cipherinfo[2])
        # WSGI specific variables
        env['wsgi.version'] = (1, 0)
        env['wsgi.errors'] = ErrorStream(self._log)
        env['wsgi.multithread'] = True
        env['wsgi.multiprocess'] = True
        env['wsgi.run_once'] = False
        # Gruvi specific variables
        env['gruvi.transport'] = self._transport
        env['gruvi.protocol'] = self._protocol
        env['gruvi.sockname'] = sockname
        env['gruvi.peername'] = self._transport.get_extra_info('peername')

    def update_environ(self):
        m = self._message
        env = self._environ
        env['SERVER_PROTOCOL'] = 'HTTP/' + m.version
        env['REQUEST_METHOD'] = m.method
        env['PATH_INFO'] = m.parsed_url[2]
        env['QUERY_STRING'] = m.parsed_url[4]
        for field, value in m.headers:
            name = field.upper().replace('-', '_')
            if name != 'CONTENT_LENGTH' and name != 'CONTENT_TYPE':
                name = 'HTTP_' + name
            env[name] = value
        env['REQUEST_URI'] = m.url
        env['wsgi.input'] = m.body
        # Support the de-facto X-Forwarded-For and X-Forwarded-Proto headers
        # that are added by reverse proxies.
        peername = env['gruvi.peername']
        remote = env.get('HTTP_X_FORWARDED_FOR')
        env['REMOTE_ADDR'] = remote if remote else peername[0] \
                                        if isinstance(peername, tuple) else ''
        proto = env.get('HTTP_X_FORWARDED_PROTO')
        env['wsgi.url_scheme'] = proto if proto else 'https' \
                                        if env.get('HTTPS') else 'http'
        env['REQUEST_SCHEME'] = env['wsgi.url_scheme']


class HttpProtocol(MessageProtocol):
    """HTTP protocol implementation."""

    identifier = 'gruvi.http'

    # Max header size. This should be enough for most things.
    # The parser keeps the header in memory during parsing.
    max_header_size = 65536

    # Max number of body bytes to buffer
    max_buffer_size = 65536

    # Max number of pipelined requests to keep before pausing the transport.
    max_pipeline_size = 10

    # In theory, max memory is pipeline_size * (header_size + buffer_size)

    def __init__(self, application=None, server_side=False, server_name=None,
                 version='1.1', timeout=None):
        """
        The *application* argument specifies a WSGI application for this
        protocol.

        The *server_side* argument specifies whether this is a client or server
        side protocol.

        The *server_name* argument can be used to override the server name for
        server side protocols. If not provided, then the socket name of the
        listening socket will be used.
        """
        if server_side and not application:
            raise ValueError('application is required for server-side protocol')
        super(HttpProtocol, self).__init__(server_side, timeout=timeout)
        self._server_side = server_side
        self._message_handler = WsgiHandler(application) if server_side else None
        self._server_name = server_name
        if version not in ('1.0', '1.1'):
            raise ValueError('version: unsupported version {!r}'.format(version))
        self._version = version
        self._create_parser()
        self._requests = []
        self._response = None
        self._writer = None
        self._message = None
        self._error = None

    @property
    def server_side(self):
        """Return whether the protocol is server-side."""
        return self._server_side

    @property
    def server_name(self):
        """Return the server name."""
        return self._server_name

    def _create_parser(self):
        # Create a new CFFI http-parser and settings object that is hooked to
        # our callbacks.
        self._parser = ffi.new('http_parser *')
        self._chandle = ffi.new_handle(self)  # store in instance to keep alive
        self._parser.data = self._chandle  # struct field doesn't take reference
        kind = lib.HTTP_REQUEST if self._server_side else lib.HTTP_RESPONSE
        lib.http_parser_init(self._parser, kind)

    def _append_header_field(self, buf):
        # Add a chunk to a header field.
        if self._header_value:
            # This starts a new header: stash away the previous one.
            # The header might be part of the http headers or trailers.
            header = (_ba2s(self._header_field), _ba2s(self._header_value))
            if self._message.body:
                self._message.trailers.append(header)
            else:
                self._message.headers.append(header)
            del self._header_field[:]
            del self._header_value[:]
        self._header_field.extend(buf)

    def _append_header_value(self, buf):
        # Add a chunk to a header value.
        if buf:
            self._header_value.extend(buf)
            return
        # If buf is empty, then complete a header set that is in progress.
        # This is called by both on_headers_complete() (for http headers) and
        # on_message_complete() (for http trailers).
        if self._header_field:
            header = (_ba2s(self._header_field), _ba2s(self._header_value))
            if self._message.body:
                self._message.trailers.append(header)
            else:
                self._message.headers.append(header)
            del self._header_field[:]
            del self._header_value[:]

    # Parser callbacks. Callbacks are run in the hub fiber, so we only do
    # parsing here. For server protocols we stash away the result in a queue
    # to be processed in a dispatcher fiber (one per protocol).
    #
    # Callbacks return 0 for success, 1 for error.

    @ffi.callback('http_cb')
    def on_message_begin(parser):
        # http-parser callback: prepare for a new message
        self = ffi.from_handle(parser.data)
        self._url = bytearray()
        self._header = bytearray()
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._header_size = 0
        self._message = HttpMessage()
        return 0

    @ffi.callback('http_data_cb')
    def on_url(parser, at, length):
        # http-parser callback: got a piece of the URL
        self = ffi.from_handle(parser.data)
        self._header_size += length
        if self._header_size > self.max_header_size:
            self._error = HttpError('HTTP header too large')
            return 1
        self._url.extend(ffi.buffer(at, length))
        return 0

    @ffi.callback('http_data_cb')
    def on_header_field(parser, at, length):
        # http-parser callback: got a piece of a header field (name)
        self = ffi.from_handle(parser.data)
        self._header_size += length
        if self._header_size > self.max_header_size:
            self._error = HttpError('HTTP header too large')
            return 1
        buf = ffi.buffer(at, length)
        self._append_header_field(buf)
        return 0

    @ffi.callback('http_data_cb')
    def on_header_value(parser, at, length):
        # http-parser callback: got a piece of a header value
        self = ffi.from_handle(parser.data)
        self._header_size += length
        if self._header_size > self.max_header_size:
            self._error = HttpError('HTTP header too large')
            return 1
        buf = ffi.buffer(at, length)
        self._append_header_value(buf)
        return 0

    @ffi.callback('http_cb')
    def on_headers_complete(parser):
        # http-parser callback: the HTTP header is complete. This is the point
        # where we hand off the message to our consumer. Going forward,
        # on_body() will continue to write chunks of the body to message.body.
        self = ffi.from_handle(parser.data)
        self._append_header_value(b'')
        m = self._message
        m.message_type = lib.http_message_type(parser)
        m.version = '{}.{}'.format(parser.http_major, parser.http_minor)
        if self._server_side:
            m.method = _http_methods.get(parser.method, '<unknown>')
            m.url = _ba2s(self._url)
            try:
                m.parsed_url = urlsplit(m.url)
            except ValueError as e:
                self._error = HttpError('urlsplit(): {!s}'.format(e))
                return 2  # error
            m.is_upgrade = lib.http_is_upgrade(parser)
        else:
            m.status_code = parser.status_code
        m.should_keep_alive = lib.http_should_keep_alive(parser)
        m.body = Stream(self._transport, 'r')
        m.body.buffer.set_buffer_limits(self.max_buffer_size)
        # Make the message available on the queue.
        self._queue.put_nowait(m)
        # Return 1 if this is a HEAD request, 0 otherwise. This instructs the
        # parser whether or not a body follows that it needs to parse.
        if not self._requests:
            return 0
        return 1 if self._requests.pop(0) == 'HEAD' else 0

    @ffi.callback('http_data_cb')
    def on_body(parser, at, length):
        # http-parser callback: got a body chunk
        self = ffi.from_handle(parser.data)
        # StreamBuffer.feed() may pause the transport here if the buffer size is exceeded.
        self._message.body.buffer.feed(bytes(ffi.buffer(at, length)))
        return 0

    @ffi.callback('http_cb')
    def on_message_complete(parser):
        # http-parser callback: the http request or response ended
        # complete any trailers that might be present
        self = ffi.from_handle(parser.data)
        self._append_header_value(b'')
        self._message.body.buffer.feed_eof()
        if self._queue.qsize() >= self.max_pipeline_size:
            self._transport.pause_reading()
        return 0

    _settings = ffi.new('http_parser_settings *')
    _settings.on_message_begin = on_message_begin
    _settings.on_url = on_url
    _settings.on_header_field = on_header_field
    _settings.on_header_value = on_header_value
    _settings.on_headers_complete = on_headers_complete
    _settings.on_body = on_body
    _settings.on_message_complete = on_message_complete

    def connection_made(self, transport):
        # Protocol callback
        self._transport = transport
        self._writer = Stream(transport, 'w')

    def data_received(self, data):
        # Protocol callback
        nbytes = lib.http_parser_execute(self._parser, self._settings, data, len(data))
        if nbytes != len(data):
            msg = _cd2s(lib.http_errno_name(lib.http_errno(self._parser)))
            self._log.debug('http_parser_execute(): {}'.format(msg))
            self._error = HttpError('parse error: {}'.format(msg))
            if self._message:
                self._message.body.buffer.feed_error(self._error)
            self._queue.put_nowait(self._error)
            self._transport.close()

    def connection_lost(self, exc):
        # Protocol callback
        # Feed the EOF to the parser. It will tell us it if was unexpected.
        nbytes = lib.http_parser_execute(self._parser, self._settings, b'', 0)
        if nbytes != 0:
            msg = _cd2s(lib.http_errno_name(lib.http_errno(self._parser)))
            self._log.debug('http_parser_execute(): {}'.format(msg))
            if exc is None:
                exc = HttpError('parse error: {}'.format(msg))
            if self._message:
                self._message.body.buffer.feed_error(exc)
        self._error = exc
        self._transport = None
        if not self._server_side:
            self._queue.put_nowait(self._error)

    def message_received(self, message):
        # Protocol callback
        if self._queue.qsize() < self.max_pipeline_size:
            self._transport.resume_reading()
        self._message_handler(message, self._transport, self)

    @switchpoint
    def request(self, method, url, headers=[], body=b''):
        """Make a new HTTP request.

        The *method* argument is the HTTP method to be used. It must be
        specified as a string, for example ``'GET'`` or ``'POST'``. The *url*
        argument specifies the URL and must be a string as well.

        The optional *headers* argument specifies extra HTTP headers to use in
        the request. It must be a list of (name, value) tuples, with name and
        value a string.

        The optional *body* argument may be used to specify a body to include
        in the request. It must be a ``bytes`` or ``str`` instance, a file-like
        object, or an iterable producing ``bytes`` or ``str`` instances. The
        default value for the body is the empty string ``b''`` which sends an
        empty body. To send potentially very large bodies, use the file or
        iterator interface. Using these interfaces will send the body under the
        "chunked" transfer encoding. This has the added advantage that the body
        size does not need to be known up front.

        The body may also be the ``None``, which means that you need to send
        the request body yourself. This is explained below.

        This method sends the request header, and if a body was specified, the
        request body as well. It then returns a :class:`HttpRequest` instance.

        If however you passsed a *body* of ``None`` then you must use the
        :meth:`HttpRequest.write` and :meth:`HttpRequest.end_request` methods
        if the :class:`HttpRequest` instance to send the request body yourself.
        This functionality is only useful if you want to sent trailers with the
        HTTP "chunked" encoding. Trailers are not normally used.

        The response to the request can be obtained by calling the
        :meth:`getresponse` method.

        You may make multiple requests before reading a response. This is
        called pipelining, and can improve per request latency greatly. For
        every request that you make, you must call :meth:`getresponse` exactly
        once. The remote HTTP implementation will send by the responses in the
        same order as the requests.
        """
        if self._error:
            raise compat.saved_exc(self._error)
        elif self._transport is None:
            raise HttpError('not connected')
        self._requests.append(method)
        request = HttpRequest(self)
        request.start_request(method, url, headers, body)
        if body is None:
            return request
        if isinstance(body, bytes):
            request.write(body)
        elif hasattr(body, 'read'):
            while True:
                chunk = body.read(4096)
                if not chunk:
                    break
                request.write(chunk)
        elif hasattr(body, '__iter__'):
            for chunk in body:
                request.write(chunk)
        request.end_request()

    @switchpoint
    def getresponse(self, timeout=-1):
        """Wait for and return a HTTP response.

        The return value is a :class:`HttpResponse` instance. When this method
        returns, only the response header has been read. The response body can
        be read using the :meth:`HttpResponse.read` and similar methods.

        Note that it is required that you read the entire body of each
        response if you use HTTP pipelining. Specifically, it is an error to
        call :meth:`getresponse` when the body of the response returned by a
        previous invocation has not yet been fully read.
        """
        if self._error:
            raise compat.saved_exc(self._error)
        elif self._transport is None:
            raise HttpError('not connected')
        if not self._requests and not self._queue.qsize():
            raise RuntimeError('there are no outstanding requests')
        if timeout < 0:
            timeout = self._timeout
        if self._response and not self._response.body.buffer.eof:
            raise RuntimeError('body of previous response not completely read')
        message = self._queue.get(timeout=timeout)
        if isinstance(message, Exception):
            raise compat.saved_exc(message)
        self._response = message
        return HttpResponse(message)


class HttpClient(Client):
    """A HTTP client."""

    def __init__(self, timeout=None):
        """The optional *timeout* argument can be used to specify a timeout for
        the various network operations used within the client."""
        super(HttpClient, self).__init__(HttpProtocol, timeout=timeout)
        self._server_name = None

    def connect(self, address, **kwargs):
        # Capture the host name that we are connecting to. We need this for
        # generating "Host" headers in HTTP/1.1
        if isinstance(address, tuple):
            host, port = address[:2]  # len(address) == 4 for IPv6
            default_port = (port == HTTP_PORT and 'ssl' not in kwargs) \
                                or (port == HTTPS_PORT and 'ssl' in kwargs)
            if not default_port:
                host = '{}:{}'.format(host, port)
            self._server_name = host
        super(HttpClient, self).connect(address, **kwargs)
        self.protocol._server_name = self._server_name

    protocol = Client.protocol

    delegate_method(protocol, HttpProtocol.request)
    delegate_method(protocol, HttpProtocol.getresponse)


class HttpServer(Server):
    """A HTTP server."""

    def __init__(self, application, server_name=None, timeout=None):
        """The constructor takes the following arguments.  The *wsgi_handler*
        argument must be a WSGI callable. See :pep:`333`.

        The optional *server_name* argument can be used to specify a server
        name. This might be needed by the WSGI application to construct
        absolute URLs. If not provided, then the host portion of the address
        passed to :meth:`~gruvi.Server.listen` will be used.

        The optional *timeout* argument can be used to specify a timeout for
        the various network operations used within the server.
        """
        protocol_factory = functools.partial(HttpProtocol, application,
                                             server_side=True, server_name=server_name)
        super(HttpServer, self).__init__(protocol_factory, timeout)
