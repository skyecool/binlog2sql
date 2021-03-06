#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import pymysql
from pymysqlreplication import BinLogStreamReader
from binlog2sql_util import (
    PY_VERSION,
    command_line_args,
    is_dml_event,
    is_ddl_event,
    generate_sql,
    type_convert,
    print_line,
)
import json
import arrow


class Binlog2sql(object):

    def __init__(self, connection_settings, start_file=None, stop_file=None, start_position=None, stop_position=None,
                 start_time=None, stop_time=None, databases=None, tables=None, no_pk=False,
                 flashback=False, stop_never=False, output_file=None, only_dml=False, sql_type=None, json=False,
                 debug=False):
        """
        conn_setting: {'host': 127.0.0.1, 'port': 3306, 'user': user, 'passwd': passwd, 'charset': 'utf8'}
        """

        # connect setting
        connection_settings.update({'charset': 'utf8'})
        self.conn_setting = connection_settings

        # interval filter
        self.start_file = start_file or None
        self.stop_file = stop_file or self.start_file
        self.start_position = start_position or 4  # use binlog v4
        self.stop_position = stop_position
        self.timezone = arrow.now().tzinfo
        self.seconds = arrow.now().utcoffset().seconds
        self.start_time = start_time and arrow.get(start_time).timestamp - self.seconds or arrow.get().min.timestamp
        self.stop_time = stop_time and arrow.get(stop_time).timestamp - self.seconds or arrow.get().max.timestamp

        # schema filter
        self.databases = databases
        self.tables = tables

        # type filter
        self.only_dml = only_dml
        self.sql_type = sql_type and [t.upper() for t in sql_type] or ['INSERT', 'UPDATE', 'DELETE']

        # optional
        self.no_pk, self.flashback, self.stop_never, self.output_file, self.json, self.debug = (
            no_pk, flashback, stop_never, output_file, json, debug
        )
        self.py_version = PY_VERSION
        self.connection = pymysql.connect(**self.conn_setting)

        with self.connection as cursor:
            cursor.execute("SHOW MASTER LOGS")
            rows = cursor.fetchall()
            if self.start_file:
                for row in rows:
                    if self.start_file <= row[0] <= self.stop_file:
                        stop_file, stop_position = row
                if stop_file:
                    self.stop_file, self.stop_position = stop_file, stop_position
                else:
                    raise ValueError('parameter error: start_file %s not in mysql server' % self.start_file)
            else:
                self.stop_file, self.stop_position = rows[-1]
                self.start_file = self.stop_file

            cursor.execute("SELECT @@server_id")
            self.server_id = cursor.fetchone()[0]
            if not self.server_id:
                raise ValueError('missing server_id in %s:%s' % (self.conn_setting['host'], self.conn_setting['port']))

    def process_binlog(self):
        if self.debug:
            for k, v in vars(self).items():
                if k == 'timezone':
                    print_line('{} = {}'.format(k, v._std_offset))
                elif k == 'connection':
                    pass
                else:
                    print_line('{} = {}'.format(k, v))
            return
        stream = BinLogStreamReader(connection_settings=self.conn_setting, server_id=self.server_id,
                                    log_file=self.start_file, log_pos=self.start_position,
                                    only_schemas=self.databases, only_tables=self.tables, resume_stream=True,
                                    blocking=True, skip_to_timestamp=self.start_time)
        with self.connection as cursor:
            sql = '# {} #\n# {} binlog2sql start! #\n# {} #'.format('=' * 50, arrow.now(), '=' * 50)
            print_line(sql, self.output_file)

            start_pos, print_interval, print_time = 4, 60 * 10, 0
            for binlog_event in stream:

                if (print_time + print_interval) < binlog_event.timestamp < self.start_time:
                    print_time = binlog_event.timestamp
                    sql = '# Binlog scan to {}'.format(arrow.get(print_time).to(self.timezone))
                    print_line(sql, self.output_file)

                if binlog_event.timestamp < self.start_time:
                    continue

                # dml
                if is_dml_event(binlog_event) in self.sql_type:
                    for row in binlog_event.rows:
                        if self.json:
                            for column in binlog_event.columns:
                                if column.type == 245:
                                    for k, v in row.items():
                                        row[k][column.name] = json.dumps(type_convert(v[column.name]),
                                                                         ensure_ascii=False)
                        sql = generate_sql(cursor=cursor, binlog_event=binlog_event, no_pk=self.no_pk, row=row,
                                           e_start_pos=start_pos, flashback=self.flashback)
                        print_line(sql, self.output_file)
                # ddl
                elif is_ddl_event(binlog_event):
                    start_pos = binlog_event.packet.log_pos
                    if not self.only_dml and binlog_event.query != 'BEGIN':
                        sql = generate_sql(cursor=cursor, binlog_event=binlog_event, no_pk=self.no_pk,
                                           e_start_pos=start_pos, flashback=self.flashback)
                        print_line(sql, self.output_file)

                # exceed the end position of the end binlog file
                if stream.log_file == self.stop_file and (
                        binlog_event.packet.log_pos >= self.stop_position or binlog_event.timestamp >= self.stop_time
                ) and not self.stop_never:
                    sql = '# {} #\n# {} binlog2sql stop!  #\n# {} #'.format('=' * 50, arrow.now(), '=' * 50)
                    print_line(sql, self.output_file)
                    break
            stream.close()


if __name__ == '__main__':
    args = command_line_args(sys.argv[1:])
    conn_setting = {'host': args.host, 'port': args.port, 'user': args.user, 'passwd': args.password}
    binlog2sql = Binlog2sql(connection_settings=conn_setting, start_file=args.start_file, stop_file=args.stop_file,
                            start_position=args.start_position, stop_position=args.stop_position,
                            start_time=args.start_time,
                            stop_time=args.stop_time,
                            databases=args.databases, tables=args.tables,
                            only_dml=args.only_dml, sql_type=args.sql_type,
                            no_pk=args.no_pk, flashback=args.flashback, stop_never=args.stop_never,
                            output_file=args.output_file, json=args.json,
                            debug=args.debug)
    binlog2sql.process_binlog()

    # conn_setting = {'host': '127.0.0.1', 'port': 3306, 'user': 'root', 'passwd': '123100'}
    # binlog2sql = Binlog2sql(connection_settings=conn_setting, json=True, output_file='backup.sql', start_time='2019-04-07 10:00:00')
    # binlog2sql.process_binlog()
