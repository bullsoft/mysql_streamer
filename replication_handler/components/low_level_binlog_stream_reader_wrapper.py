# -*- coding: utf-8 -*-
# Copyright 2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import absolute_import
from __future__ import unicode_literals

import logging
import random

from data_pipeline.message import CreateMessage
from data_pipeline.message import DeleteMessage
from data_pipeline.message import RefreshMessage
from data_pipeline.message import UpdateMessage
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.constants.BINLOG import DELETE_ROWS_EVENT_V2
from pymysqlreplication.constants.BINLOG import UPDATE_ROWS_EVENT_V2
from pymysqlreplication.constants.BINLOG import WRITE_ROWS_EVENT_V2
from pymysqlreplication.event import GtidEvent
from pymysqlreplication.event import QueryEvent
from pymysqlreplication.row_event import DeleteRowsEvent
from pymysqlreplication.row_event import UpdateRowsEvent
from pymysqlreplication.row_event import WriteRowsEvent

from replication_handler import config
from replication_handler.components.base_binlog_stream_reader_wrapper import BaseBinlogStreamReaderWrapper
from replication_handler.util.misc import DataEvent


log = logging.getLogger('replication_handler.components.low_level_binlog_stream_reader_wrapper')


message_type_map = {
    WRITE_ROWS_EVENT_V2: CreateMessage,
    UPDATE_ROWS_EVENT_V2: UpdateMessage,
    DELETE_ROWS_EVENT_V2: DeleteMessage,
}


class LowLevelBinlogStreamReaderWrapper(BaseBinlogStreamReaderWrapper):
    """ This class wraps pymysqlreplication stream object, providing the ability to
    resume stream at a specific position, peek at next event, and pop next event.

    Args:
      position(Position object): use to specify where the stream should resume.
    """

    def __init__(self, source_database_config, tracker_database_config, position):
        super(LowLevelBinlogStreamReaderWrapper, self).__init__()
        self.refresh_table_suffix = '_data_pipeline_refresh'
        only_tables = self._get_only_tables()
        allowed_event_types = [
            GtidEvent,
            QueryEvent,
            WriteRowsEvent,
            UpdateRowsEvent,
            DeleteRowsEvent,
        ]
        self._seek(
            source_database_config,
            tracker_database_config,
            allowed_event_types,
            position,
            only_tables
        )

    def _get_only_tables(self):
        only_tables = config.env_config.table_whitelist
        if not only_tables:
            return None
        res_only_table = []
        for table_name in only_tables:
            # prevents us from whitelisting a refresh table
            # without the underlying table being whitelisted
            if table_name.endswith(self.refresh_table_suffix):
                continue
            res_only_table.append(table_name)
            res_only_table.append("{0}{1}".format(
                table_name,
                self.refresh_table_suffix
            ))

        return res_only_table

    def _refill_current_events(self):
        if not self.current_events:
            self.current_events.extend(self._prepare_event(self.stream.fetchone()))

    def _prepare_event(self, event):
        """ event can be None, see http://bit.ly/1JaLW9G."""
        if event:
            if isinstance(event, (QueryEvent, GtidEvent)):
                # TODO(cheng|DATAPIPE-173): log_pos and log_file is useful information
                # to have on events, we will decide if we want to remove this when gtid is
                # enabled if the future.
                event.log_pos = self.stream.log_pos
                event.log_file = self.stream.log_file
                return [event]
            elif isinstance(event, (WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent)):
                return self._get_data_events_from_row_event(event)
        return []

    def _get_data_events_from_row_event(self, row_event):
        """ Convert the rows into events."""
        target_table = row_event.table
        message_type = message_type_map[row_event.event_type]
        # Tables with suffix _data_pipeline_refresh come
        # from the FullRefreshRunner.
        if row_event.table.endswith(self.refresh_table_suffix):
            # Table that this row_event is meant for
            # is determined by removing the suffix.
            target_table = row_event.table[:-len(self.refresh_table_suffix)]
            message_type = RefreshMessage
        return [
            DataEvent(
                schema=row_event.schema,
                table=target_table,
                log_pos=self.stream.log_pos,
                log_file=self.stream.log_file,
                row=row,
                timestamp=row_event.timestamp,
                message_type=message_type
            ) for row in row_event.rows
        ]

    def get_unique_server_id(self):
        # server_id must be unique per instance
        MIN_SERVER_ID = 1
        MAX_SERVER_ID = 4294967295
        return random.randint(MIN_SERVER_ID, MAX_SERVER_ID)

    def _seek(
        self,
        source_database_config,
        tracker_database_config,
        allowed_event_types,
        position,
        only_tables
    ):
        self.stream = BinLogStreamReader(
            connection_settings=source_database_config,
            ctl_connection_settings=tracker_database_config,
            server_id=self.get_unique_server_id(),
            blocking=True,
            only_events=allowed_event_types,
            resume_stream=config.env_config.resume_stream,
            only_tables=only_tables,
            fail_on_table_metadata_unavailable=True,
            **position.to_replication_dict()
        )
