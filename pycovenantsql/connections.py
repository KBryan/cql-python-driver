import sys
import os
import requests
from . import err
from .cursors import Cursor
from .optionfile import Parser
from ._compat import PY2, range_type, text_type, str_type, JYTHON, IRONPYTHON


class Connection(object):
    """
    Representation of a RPC connect with a CovenantSQL server.

    The proper way to get an instance of this class is to call
    connect().

    Establish a connection to the CovenantSQL database. Accepts several
    arguments:

    :param uri: Uri address starts with "covenantsql://", where the database id is located
    :param key: Private key to access database.
    :param database: Database id to use. uri and database should set at least one.
    :param read_timeout: The timeout for reading from the connection in seconds (default: None - no timeout)
    :param write_timeout: The timeout for writing to the connection in seconds (default: None - no timeout)
    :param read_default_file:
        Specifies  my.cnf file to read these parameters from under the [client] section.
    :param use_unicode:
        Whether or not to default to unicode strings.
        This option defaults to true for Py3k.
    :param cursorclass: Custom cursor class to use.
    :param connect_timeout: Timeout before throwing an exception when connecting.
        (default: 10, min: 1, max: 31536000)
    :param read_default_group: Group to read from in the configuration file.
    :param autocommit: Autocommit mode. None means use server default. (default: False)
    :param defer_connect: Don't explicitly connect on contruction - wait for connect call.
        (default: False)

    See `Connection <https://www.python.org/dev/peps/pep-0249/#connection-objects>`_ in the
    specification.
    """


    _closed = False

    def __init__(self, dsn=None, host=None, port=0, key=None, database=None,
                 https_pem=None, read_default_file=None, use_unicode=None,
                 cursorclass=Cursor, init_command=None,
                 connect_timeout=10, read_default_group=None,
                 autocommit=False, defer_connect=False,
                 read_timeout=None, write_timeout=None):

        self._resp = None

        # 1. pre process params in init
        if use_unicode is None and sys.version_info[0] > 2:
            use_unicode = True
        self.encoding = 'utf8'

        # 2. read config params from file(if init is None)
        if read_default_group and not read_default_file:
            read_default_file = "covenant.cnf"

        if read_default_file:
            if not read_default_group:
                read_default_group = "python-client"

            cfg = Parser()
            cfg.read(os.path.expanduser(read_default_file))

            def _config(key, arg):
                if arg:
                    return arg
                try:
                    return cfg.get(read_default_group, key)
                except Exception:
                    return arg

            dsn = _config("dsn", dsn)
            host = _config("host", host)
            port = int(_config("port", port))
            key = _config("key", key)
            database = _config("database", database)
            https_pem = _config("https_pem", https_pem)

        # 3. save params
        self.dsn = dsn
        # TODO dsn parse to host, port and database
        self.host = host or "localhost"
        self.port = port or 11108
        self.key = key
        self.database = database

        self._query_uri = "https://" + self.host + ":" + str(self.port) + "/v1/query"
        self._exec_uri = "https://" + self.host + ":" + str(self.port) + "/v1/exec"

        self._session = requests.Session()
        self._session.verify = False
        requests.packages.urllib3.disable_warnings()
        if https_pem:
            self._session.cert = (https_pem, self.key)
        else:
            self._session.cert = self.key

        if not (0 < connect_timeout <= 31536000):
            raise ValueError("connect_timeout should be >0 and <=31536000")
        self.connect_timeout = connect_timeout or None
        if read_timeout is not None and read_timeout <= 0:
            raise ValueError("read_timeout should be >= 0")
        self._read_timeout = read_timeout
        if write_timeout is not None and write_timeout <= 0:
            raise ValueError("write_timeout should be >= 0")
        self._write_timeout = write_timeout

        if use_unicode is not None:
            self.use_unicode = use_unicode

        self.cursorclass = cursorclass

        self._result = None
        self._affected_rows = 0

        #: specified autocommit mode. None means use server default.
        self.autocommit_mode = autocommit

        if defer_connect:
            self._sock = None
        else:
            self.connect()

    def connect(self):
        self._closed = False
        self._sock = True
        self._execute_command("select 1;")
        self._read_ok_packet()

    def close(self):
        """
        Send the quit message and close the socket.

        See `Connection.close() <https://www.python.org/dev/peps/pep-0249/#Connection.close>`_
        in the specification.

        :raise Error: If the connection is already closed.
        """
        if self._closed:
            raise err.Error("Already closed")
        self._sock = None
        self._closed = True

    def commit(self):
        """
        Commit changes to stable storage.

        See `Connection.commit() <https://www.python.org/dev/peps/pep-0249/#commit>`_
        in the specification.
        """
        self._execute_command("COMMIT")
        self._read_ok_packet()

    def rollback(self):
        """
        Roll back the current transaction.

        See `Connection.rollback() <https://www.python.org/dev/peps/pep-0249/#rollback>`_
        in the specification.
        """
        self._execute_command("ROLLBACK")
        self._read_ok_packet()


    def cursor(self, cursor=None):
        """
        Create a new cursor to execute queries with.

        :param cursor: The type of cursor to create; current only :py:class:`Cursor`
            None means use Cursor.
        """
        if cursor:
            return cursor(self)
        return self.cursorclass(self)


    # The following methods are INTERNAL USE ONLY (called from Cursor)
    def query(self, sql):
        # if DEBUG:
        #     print("DEBUG: sending query:", sql)
        if isinstance(sql, text_type) and not (JYTHON or IRONPYTHON):
            if PY2:
                sql = sql.encode(self.encoding)
            else:
                sql = sql.encode(self.encoding, 'surrogateescape')
        self._execute_command(sql)
        self._affected_rows = self._read_query_result()
        return self._affected_rows

    def _execute_command(self, sql):
        """
        :raise InterfaceError: If the connection is closed.
        :raise ValueError: If no username was specified.
        """
        if self._closed:
            raise err.InterfaceError("Connection closed")

        if isinstance(sql, text_type):
            sql = sql.encode(self.encoding)

        # drop last command return
        if self._resp is not None:
            self._resp = None

        # post request
        data = {"database": self.database,"query": sql}
        try:
            if sql.lower().lstrip().startswith(b'select'):
                self._resp = self._session.post(self._query_uri, data=data)
            else:
                self._resp = self._session.post(self._exec_uri, data=data)
        except Exception as error:
            raise err.InterfaceError("Request proxy err: %s" % error)

        try:
            self._resp_json = self._resp.json()
        except Exception as error:
            raise err.InterfaceError("Proxy return invalid data", self._resp.reason)


    def _read_query_result(self):
        self._result = None
        self._read_ok_packet()
        result = CovenantSQLResult(self)
        result.read()
        self._result = result
        return result.affected_rows

    def _read_ok_packet(self):
        self.server_status = self._resp_json["success"]
        if not self.server_status:
            raise err.OperationalError("Syntax error", self._resp_json["status"])

        if not self._resp.ok:
            raise err.OperationalError("Proxy return false", self._resp.reason)

        return self.server_status

class CovenantSQLResult(object):
    def __init__(self, connection):
        """
        :type connection: Connection
        """
        self.connection = connection
        self.affected_rows = None
        self.insert_id = None
        self.warning_count = 0
        self.message = None
        self.field_count = 0
        self.description = None
        self.rows = None
        self.has_next = None

    def read(self):
        try:
            data = self.connection._resp_json["data"]
        except:
            raise err.InterfaceError("Unsupported response format")
        if data is None:
            # exec result
            return
        # read json data
        self.affected_rows = len(data['rows'])
        rows = []
        for line in data['rows']:
            row = []
            for column in line:
                row.append(column)
            rows.append(tuple(row))
        self.rows = tuple(rows)