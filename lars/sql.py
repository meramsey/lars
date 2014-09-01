# vim: set et sw=4 sts=4 fileencoding=utf-8:
#
# Copyright (c) 2013 Dave Hughes <dave@waveform.org.uk>
# Copyright (c) 2013 Mime Consulting Ltd. <info@mimeconsulting.co.uk>
# All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
This module provides a target wrapper for SQL-based databases, which can
provide a powerful means of analyzing log data.

The :class:`SQLTarget` class accepts row objects in its
:meth:`~SQLTarget.write` method and automatically generates the required SQL
``INSERT`` statements to append records to the specified target table.

The implementation has been tested with SQLite3 (built into Python), and
PostgreSQL, but should work with any `PEP-249`_ (Python DB API 2.0) compatible
database cursor. A list of available Python database drives is maintained on
the Python wiki `DatabaseInterfaces`_ page.


Classes
=======

.. autoclass:: SQLTarget
    :members:

    .. attribute:: commit

        The number of rows which the class will attempt to write before
        performing a COMMIT. It is strongly recommended to set this to a
        reasonably large number (e.g. 1000) to ensure decent INSERT performance

    .. attribute:: insert

        The number of rows which the class will attempt to insert with each
        INSERT statement. The :attr:`commit` parameter must be a multiple of
        this value.

        .. versionadded:: 0.2

    .. attribute:: count

        Returns the number of rows successfully written to the database so far

    .. attribute:: create_table

        If True, the class will attempt to create the target table during the
        first call to the :meth:`write` method

    .. attribute:: drop_table

        If True, the class will attempt to unconditionally drop any existing
        target table during the first call to the :meth:`write` method

    .. attribute:: ignore_drop_errors

        If True, and :attr:`drop_table` is True, any errors encountered during
        the ``DROP TABLE`` operation will be ignored (typically useful when you
        are not sure the target table exists or not)

    .. attribute:: table

        The name of the target table in the database, including any required
        escaping or quotation


.. autoclass:: OracleTarget
    :members:


Exceptions
==========

.. autoexception:: SQLError
   :members:


Examples
========

A typical example of working with the class is shown below::

    import io
    import sqlite3
    from lars import apache, sql

    connection = sqlite3.connect('apache.db', detect_types=sqlite3.PARSE_DECLTYPES)

    with io.open('/var/log/apache2/access.log', 'rb') as infile:
        with io.open('apache.csv', 'wb') as outfile:
            with apache.ApacheSource(infile) as source:
                with sql.SQLTarget(sqlite3, connection, 'log_entries', create_table=True) as target:
                    for row in source:
                        target.write(row)

.. _PEP-249: http://www.python.org/dev/peps/pep-0249/
.. _DatabaseInterfaces: http://wiki.python.org/moin/DatabaseInterfaces
"""

from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )
str = type('')


import warnings
import logging
try:
    import ipaddress
except ImportError:
    import ipaddr as ipaddress
import sqlite3
from datetime import date, time, datetime

from lars import datatypes


class SQLError(Exception):
    """
    Base class for all fatal errors generated by classes in the sql module.

    Exceptions of this class take the optional argument row for specifying the
    row (if any) that was being inserted (or retrieved) when the error
    occurred. If specified, the :meth:`__str__` method is overridden to include
    the row in the error message.

    :param str message: The error message
    :param row: The row being processed when the error occurred
    """
    def __init__(self, message, row=None):
        self.row = row
        super(SQLError, self).__init__(message)

    def __str__(self):
        result = super(SQLError, self).__str__()
        if self.row:
            result = '%s while processing row %s' % (result, self.row)
        return result


class SQLTarget(object):
    """
    Wraps a database connection to insert row tuples into an SQL database
    table.

    This wrapper provides a simple :meth:`write` method which can be used to
    insert row tuples into a specified table, which can optionally by created
    automatically by the wrapper before insertion of the first row. The wrapper
    must be passed a database connection object that conforms to the Python
    DB-API (version 2.0) as defined by `PEP-249`_.

    The *db_module* parameter must be passed the module that defines the
    database interface (this odd requirement is so that the wrapper can look up
    the parameter style that the interface uses, and the exceptions that it
    declares).

    The *connection* parameter must be given an active database connection
    object (presumably belonging to the module passed to *db_module*).

    The *table* parameter is the final mandatory parameter which names the
    table that values are to be inserted into. If the table name requires
    quoting in the target SQL dialect, you should include such quoting in the
    *table* value (this class does not try and discern what database engine
    you are connecting to and thus has no idea about non-standard quoting
    styles like ```MySQL``` or ``[MS-SQL]``).

    The *insert* parameter controls how many rows are inserted in a single
    ``INSERT`` statement. If this is set to a value greater than 1 (the
    default), then the :meth:`write` method will buffer rows until the count
    is reached and attempt to insert all rows at once.

    .. versionadded:: 0.2

    .. warning::

        This is a relatively risky option. If an error occurs while inserting
        one of the rows in a multi-row insert, then normally *all* rows in the
        buffer will fail to be inserted, but you will not be able to determine
        (in your script) which row caused the failure, or which rows should be
        re-attempted.

        In other words, only use this if you are certain that failures cannot
        occur during insertion (e.g. if the target table has no constraints,
        no primary/unique keys, and no triggers which might signal failure).

    The *commit* parameter controls how often a ``COMMIT`` statement is
    executed when inserting rows. By default, this is 1000 which is usually
    sufficient to provide decent performance but may (in certain database
    engines with fixed size transaction logs) cause errors, in which case you
    may wish to specify a lower value. This parameter *must* be a multiple of
    the value of the *insert* parameter (otherwise, the ``COMMIT`` statement
    will not be run reliably).

    If the *create_table* parameter is set to True (it defaults to False), when
    the :meth:`write` method is first called, the class will determine column
    names and types from the row passed in and will attempt to generate and
    execute a ``CREATE TABLE`` statement to set up the target table
    automatically. The database types that are used in the ``CREATE TABLE``
    statement are controlled by other optional parameters and are documented in
    the table below:

    +-----------------+-------------------------------------------------------+
    | Parameter       | Default Value (SQL)                                   |
    +=================+=======================================================+
    | *str_type*      | ``VARCHAR(1000)`` - typically used for URL fields.    |
    +-----------------+-------------------------------------------------------+
    | *int_type*      | ``INTEGER`` - used for fields like status and size.   |
    |                 | If your server is serving large binaries you may wish |
    |                 | to use a 64-bit type like ``BIGINT`` here instead.    |
    +-----------------+-------------------------------------------------------+
    | *fixed_type*    | ``DOUBLE`` - used for fields like time_taken. Some    |
    |                 | users may wish to change this an appropriate          |
    |                 | ``NUMERIC`` or ``DECIMAL`` specification for          |
    |                 | precision.                                            |
    +-----------------+-------------------------------------------------------+
    | *bool_type*     | ``SMALLINT`` - used for any boolean values in the     |
    |                 | input (0 for False, 1 for True)                       |
    +-----------------+-------------------------------------------------------+
    | *date_type*     | ``DATE``                                              |
    +-----------------+-------------------------------------------------------+
    | *time_type*     | ``TIME``                                              |
    +-----------------+-------------------------------------------------------+
    | *datetime_type* | ``TIMESTAMP`` - MS-SQL users will likely wish to      |
    |                 | change this to ``DATETIME`` or ``SMALLDATETIME``.     |
    |                 | MySQL users may wish to change this to ``DATETIME``,  |
    |                 | although ``TIMESTAMP`` is technically also supported  |
    |                 | (albeit with functional differences).                 |
    +-----------------+-------------------------------------------------------+
    | *ip_type*       | ``VARCHAR(53)`` - this is sufficient for storing all  |
    |                 | possible IP address and port combinations up and      |
    |                 | including an IPv6 v4-mapped address. If you are       |
    |                 | certain you will only need IPv4 support you may wish  |
    |                 | to use a length of 21 (with port) or 15 (no port).    |
    |                 | PostgreSQL users may wish to use the special ``inet`` |
    |                 | type instead as this is much more efficient but       |
    |                 | cannot store port information.                        |
    +-----------------+-------------------------------------------------------+
    | *hostname_type* | ``VARCHAR(255)``                                      |
    +-----------------+-------------------------------------------------------+
    | *path_type*     | ``VARCHAR(260)``                                      |
    +-----------------+-------------------------------------------------------+

    If the *drop_table* parameter is set to True (it defaults to False), the
    wrapper will first attempt to use ``DROP TABLE`` to destroy any existing
    table before attempting ``CREATE TABLE``. If *ignore_drop_errors* is
    True (which it is by default) then any errors encountered during the drop
    operation (e.g. if the table does not exist) will be ignored.
    """

    def __init__(
            self, db_module, connection, table, insert=1, commit=1000,
            create_table=False, drop_table=False, ignore_drop_errors=True,
            str_type='VARCHAR(1000)', int_type='INTEGER', fixed_type='DOUBLE',
            bool_type='SMALLINT', date_type='DATE', time_type='TIME',
            datetime_type='TIMESTAMP', ip_type='VARCHAR(53)',
            hostname_type='VARCHAR(255)', path_type='VARCHAR(260)'):
        if not hasattr(db_module, 'paramstyle'):
            raise NameError('The database module has no "paramstyle" global')
        if not hasattr(db_module, 'Error'):
            raise NameError('The database module has no "Error" class')
        self.db_module = db_module
        self.connection = connection
        self.table = table
        if insert < 1:
            raise ValueError('insert must be 1 or more')
        self.insert = insert
        if commit < 1:
            raise ValueError('commit must be 1 or more')
        if (commit % insert) != 0:
            raise ValueError('commit must be a multiple of %d' % insert)
        self.commit = commit
        self.create_table = create_table
        self.drop_table = drop_table
        self.ignore_drop_errors = ignore_drop_errors
        self.type_map = {
            # Python base types
            str:                   str_type,
            int:                   int_type,
            float:                 fixed_type,
            bool:                  bool_type,
            date:                  date_type,
            time:                  time_type,
            datetime:              datetime_type,
            ipaddress.IPv4Address: ip_type,
            ipaddress.IPv6Address: ip_type,
            # lars types
            datatypes.Date:        date_type,
            datatypes.Time:        time_type,
            datatypes.DateTime:    datetime_type,
            datatypes.Url:         str_type,
            datatypes.IPv4Address: ip_type,
            datatypes.IPv6Address: ip_type,
            datatypes.IPv4Port:    ip_type,
            datatypes.IPv6Port:    ip_type,
            datatypes.Hostname:    hostname_type,
            datatypes.Path:        path_type,
            }
        self.count = 0
        self._buffer = []
        self._first_row = None
        self._row_casts = None
        self._cursor = None
        self._statement = None

    def __enter__(self):
        logging.debug('Entering SQL context')
        logging.debug('Constructing cursor')
        self.count = 0
        self._cursor = self.connection.cursor()
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        logging.debug('Exiting SQL context')
        if self._buffer:
            logging.debug('Clearing %d rows in buffer', len(self._buffer))
            self._statement = self._generate_statement(
                self._first_row, len(self._buffer)
                )
            try:
                self._insert_buffer()
            except self.db_module.Error as exc:
                raise SQLError(str(exc))
        logging.debug('Closing cursor')
        self._cursor.close()
        self._cursor = None
        self._first_row = None
        self._row_casts = None
        self._statement = None
        logging.debug('COMMIT')
        self.connection.commit()

    def _create_table(self, row):
        logging.debug('Creating table %s' % self.table)
        sql = 'CREATE TABLE %(table)s (%(fields)s)' % {
            'table':  self.table,
            'fields': ', '.join([
                '%(name)s %(type)s' % {
                    'name': name,
                    'type': self.type_map[type(value)],
                    }
                for (name, value) in zip(
                    row._fields if hasattr(row, '_fields') else
                    ['field%d' % (i + 1) for i in range(len(row))],
                    row)
                ]),
            }
        logging.debug(sql)
        self._cursor.execute(sql)
        logging.debug('COMMIT')
        self.connection.commit()

    def _drop_table(self):
        logging.debug('Dropping table %s' % self.table)
        sql = 'DROP TABLE %s' % self.table
        logging.debug(sql)
        self._cursor.execute(sql)
        logging.debug('COMMIT')
        self.connection.commit()

    def _insert_buffer(self):
        try:
            self._cursor.execute(self._statement, [
                value
                for params in self._buffer
                for value in params
                ])
            self.count += len(self._buffer)
        finally:
            # The buffer must be cleared, even in the event of an exception
            # occurring, to ensure that the __exit__ handler does not
            # re-attempt insertions which result in error
            del self._buffer[:]

    def _generate_statement(self, row, count=1):
        # Technically we ought to quote the table substitution below in the
        # case that self.table contains a keyword, or "unsafe" characters
        # in SQL. However, that means getting into what constitutes a
        # keyword in various engines, not to mention the myriad quoting
        # systems ([MS SQL], `MySQL`, "standard") that exist in SQL
        # implementations. Instead, we simply assume if the user wants
        # quoting, they can supply it themselves in the table parameter...
        #
        # The parameter bindings are constructed according to the provided
        # paramstyle, so here's the obligatory whinge about Python's crap
        # DB-API. Why do we have *FIVE* different paramstyles?! What's
        # wrong with the absolutely standard qmark (?) paramstyle which
        # *EVERY* database (yes, even MySQL!) supports?! Why do I have to
        # write cryptic garbage like this to construct SQL in Python?! Why
        # for that matter do I have to get the user to pass in paramstyle
        # to the constructor - why isn't it at least an attribute on the
        # connection object?! Eurgh - PEP-249 is garbage...
        values_row = '(%s)' % ', '.join([{
            'qmark':    '?',
            'numeric':  ':%d' % i,
            'named':    ':%s' % name,
            'format':   '%s',
            'pyformat': '%%(%s)s' % name,
            }[self.db_module.paramstyle]
            for (i, name) in enumerate(
                row._fields if hasattr(row, '_fields') else
                ['field%d' % (j + 1) for j in range(len(row))]
                )
            ])
        statement = 'INSERT INTO %s VALUES %s%s' % (
            self.table,
            values_row,
            (', ' + values_row) * (count - 1)
            )
        return statement

    def _generate_row_casts(self, row):
        # Bit of a dirty hack, but it seems the most user-friendly way of
        # dealing with IP addresses depending on the type selected for the
        # target table
        ip_bases = (ipaddress.IPv4Address, ipaddress.IPv6Address)
        if self.type_map[datatypes.IPv4Address].upper().startswith(('INT', 'NUM')):
            ip_cast = int
        else:
            ip_cast = str
        return [
            ip_cast if isinstance(value, ip_bases) else
            str if isinstance(value, datatypes.Url) else
            None
            for value in row
            ]

    def write(self, row):
        if self._first_row:
            if len(row) != len(self._first_row):
                raise TypeError('Rows must have the same number of elements')
        else:
            logging.debug('First row')
            self._first_row = row
            logging.debug('Constructing INSERT statement')
            self._statement = self._generate_statement(row, self.insert)
            logging.debug(
                self._statement[:120] + ('...' if len(self._statement) > 120 else '')
                )
            logging.debug('Constructing row casts')
            self._row_casts = self._generate_row_casts(row)
            if self.drop_table:
                try:
                    self._drop_table()
                except self.db_module.Error as exc:
                    if not self.ignore_drop_errors:
                        raise SQLError(str(exc))
                    logging.debug('While dropping table %s occurred', str(exc))
            if self.create_table:
                self._create_table(row)
        # XXX What about paramstyles pyformat and named? Eurgh...
        self._buffer.append([
            None if value is None else
            cast(value) if cast else
            value
            for (cast, value) in zip(self._row_casts, row)
            ])
        if len(self._buffer) >= self.insert:
            try:
                self._insert_buffer()
            except self.db_module.Error as exc:
                if self.insert == 1:
                    raise SQLError(str(exc), row)
                # The row is meaningless if we're inserting multiple rows and
                # something goes wrong
                raise SQLError(str(exc))
            if (self.count % self.commit) == 0:
                logging.debug('COMMIT')
                self.connection.commit()


class OracleTarget(SQLTarget):
    """
    The Oracle database is sufficiently peculiar (particularly in its
    non-standard syntax for multi-row INSERTs, and odd datatypes) to require
    its own sub-class of :class:`SQLTarget`. This sub-class takes all the same
    parameters as :class:`SQLTarget`, but customizes them specifically for
    Oracle, and overrides the SQL generation methods to cope with Oracle's
    strange syntax.

    .. versionadded:: 0.2
    """

    def __init__(
            self, db_module, connection, table, insert=1, commit=1000,
            create_table=False, drop_table=False, ignore_drop_errors=True,
            str_type='VARCHAR2(1000)', int_type='NUMBER(10)',
            fixed_type='NUMBER', bool_type='NUMBER(1)', date_type='DATE',
            time_type='DATE', datetime_type='DATE', ip_type='VARCHAR2(53)',
            hostname_type='VARCHAR2(255)', path_type='VARCHAR2(260)'):
        super(OracleTarget, self).__init__(
                db_module, connection, table, insert, commit, create_table,
                drop_table, ignore_drop_errors, str_type, bool_type, date_type,
                time_type, datetime_type, ip_type, hostname_type, path_type)

    def _generate_statement(self, row, count=1):
        if count == 1:
            return super(OracleTarget, self)._generate_statement(row, count)
        values_row = 'INTO %s VALUES (%s)' % (
            self.table,
            ', '.join([{
                'qmark':    '?',
                'numeric':  ':%d' % i,
                'named':    ':%s' % name,
                'format':   '%s',
                'pyformat': '%%(%s)s' % name,
                }[self.db_module.paramstyle]
                for (i, name) in enumerate(
                    row._fields if hasattr(row, '_fields') else
                    ['field%d' % (j + 1) for j in range(len(row))]
                    )
                ])
            )
        statement = 'INSERT ALL %s%s SELECT * FROM DUAL' % (
            self.table,
            values_row,
            (' ' + values_row) * (count - 1)
            )
        return statement

