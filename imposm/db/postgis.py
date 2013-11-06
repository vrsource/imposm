# Copyright 2011-2012 Omniscale (http://omniscale.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import uuid
from contextlib import contextmanager

import psycopg2
import psycopg2.extensions
import psycopg2.extras

import logging
log = logging.getLogger(__name__)

from imposm import config
from imposm.mapping import UnionView, GeneralizedTable, FixInvalidPolygons, Mapping

unknown = object()

class PostGISDB(object):
    insert_data_format = 'tuple'

    def __init__(self, db_conf, use_geometry_columns_table=unknown):
        self.db_conf = db_conf
        self.srid = int(db_conf['proj'].split(':')[1])

        self._insert_stmts = {}
        self._connection = None
        self._cur = None

        if use_geometry_columns_table is unknown:
            if self.is_postgis_2():
                use_geometry_columns_table = False
            else:
                use_geometry_columns_table = True
        self.use_geometry_columns_table = use_geometry_columns_table

    @property
    def table_prefix(self):
        if self.db_conf.prefix:
            return self.db_conf.prefix.rstrip('_') + '_'
        return self.db_conf.prefix

    def to_tablename(self, name):
        return self.table_prefix + name.lower()

    def is_postgis_2(self):
        cur = self.connection.cursor()
        cur.execute('SELECT postgis_version()')
        version_string = cur.fetchone()[0]
        return version_string.strip()[0] == '2'

    @property
    def connection(self):
        if not self._connection:
            kw = {}
            if self.db_conf.port:
                kw['port'] = int(self.db_conf.port)
            self._connection = psycopg2.connect(
                database=self.db_conf.db,
                host=self.db_conf.host,
                user=self.db_conf.user,
                password=self.db_conf.password,
                sslmode=self.db_conf.get('sslmode', 'allow'),
                **kw
            )
            self._connection.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED)
            # Register adapter and typecaster for dict conversion.
            psycopg2.extras.register_hstore(self._connection, unicode = True)
        return self._connection

    def commit(self):
        self.connection.commit()

    @property
    def cur(self):
        if self._cur is None:
            self._cur = self.connection.cursor()
        return self._cur

    @contextmanager
    def savepoint(self, cur, raise_errors=False):
        savepoint_name = 'savepoint' + uuid.uuid4().get_hex()
        try:
            cur.execute('SAVEPOINT %s' % savepoint_name)
            yield
        except psycopg2.ProgrammingError:
            cur.execute('ROLLBACK TO SAVEPOINT %s' % savepoint_name)
            if raise_errors:
                raise

    def insert(self, mapping, insert_data, tries=0):
        insert_stmt = self.insert_stmt(mapping)
        try:
            if tries:
                self.reconnect()
            self.cur.executemany(insert_stmt, insert_data)
        except psycopg2.OperationalError, ex:
            if tries >= 8:
                log.warn('%s, giving up', ex)
                raise
            seconds = 2 ** (tries + 1)
            log.warn('%s, retry in %d', ex, seconds)
            time.sleep(seconds)
            self.insert(mapping, insert_data, tries=tries + 1)
        except psycopg2.Error, ex:
            self.connection.rollback()
            for data in insert_data:
                try:
                    self.cur.execute(insert_stmt, data)
                except psycopg2.Error, ex:
                    log.warn('error while importing "%r": %s', data, ex)
                    self.connection.rollback()
                else:
                    self.connection.commit()

        self.connection.commit()

    def post_insert(self, mappings):
        mappings = [m for m in mappings.values() if isinstance(m, (GeneralizedTable, Mapping))]
        for mapping in mappings:
            table_name = self.to_tablename(mapping.name)
            self.create_geom_index(table_name)

    def create_geom_index(self, table_name):
        idx_name = '%s_geom' % table_name
        cur = self.connection.cursor()
        cur.execute("""
            CREATE INDEX "%s" ON "%s" USING GIST (geometry)
        """ % (idx_name,table_name))
        self.connection.commit()

    def geom_wrapper(self, geom):
        return psycopg2.Binary(geom.wkb)

    def reconnect(self):
        if self._connection:
            try:
                self._connection.close()
            except psycopg2.InterfaceError:
                pass
        self._connection = None
        self._cur = None

    def insert_stmt(self, mapping):
        if mapping.name not in self._insert_stmts:
            self._insert_stmts[mapping.name] = self._insert_stmt(mapping)

        return self._insert_stmts[mapping.name]

    def _insert_stmt(self, mapping):
        extra_arg_names = extra_args = ''
        if mapping.fields:
            extra_arg_names = [n for n, t in mapping.fields]
            extra_args = ', %s' * len(extra_arg_names)
            extra_arg_names = ', ' + ', '.join('"' + name + '"' for name in extra_arg_names)

        # Add tags argument for hstore.
        if mapping.use_hstore:
            extra_arg_names += ', tags'
            extra_args += ', %s'

        return """INSERT INTO "%(tablename)s"
            (osm_id, geometry %(extra_arg_names)s)
            VALUES (%%s, ST_Transform(ST_GeomFromWKB(%%s, 4326), %(srid)s)
                %(extra_args)s)
        """.strip() % dict(tablename=self.table_prefix + mapping.name, srid=self.srid,
            extra_arg_names=extra_arg_names, extra_args=extra_args)


    def create_tables(self, mappings):
        for mapping in mappings:
            self.create_table(mapping)

    def drop_table_or_view(self, cur, name):
        with self.savepoint(cur):
            cur.execute('DROP TABLE "' + name + '" CASCADE')
        with self.savepoint(cur):
            cur.execute('DROP VIEW "' + name + '" CASCADE')

    def create_table(self, mapping):
        tablename = self.table_prefix + mapping.name
        cur = self.connection.cursor()

        self.drop_table_or_view(cur, tablename)

        extra_fields = ''
        for n, t in mapping.fields:
            extra_fields += ', "%s" %s ' % (n, t.column_type)

        # Add hstore column named tags.
        if mapping.use_hstore:
            extra_fields += ', "tags" HSTORE '

        if config.imposm_pg_serial_id:
            serial_column = "id SERIAL PRIMARY KEY,"
        else:
            serial_column = ""

        cur.execute("""
            CREATE TABLE "%s" (
                %s
                osm_id BIGINT
                %s
            );
        """ % (tablename, serial_column, extra_fields))

        self.create_geometry_column(cur, tablename, mapping)

        self.create_field_indices(cur=cur, mapping=mapping, tablename=tablename)


    def create_geometry_column(self, cur, tablename, mapping):
        if self.use_geometry_columns_table:
            cur.execute("""
                SELECT AddGeometryColumn ('', '%(tablename)s', 'geometry',
                                          %(srid)s, '%(pg_geometry_type)s', 2)
            """ % dict(tablename=tablename, srid=self.srid,
                       pg_geometry_type=mapping.geom_type))
        else:
            cur.execute("""
                ALTER TABLE %(tablename)s ADD COLUMN geometry geometry(%(pg_geometry_type)s, %(srid)s);
            """ % dict(tablename=tablename, srid=self.srid,
                       pg_geometry_type=mapping.geom_type))

    def create_field_indices(self, cur, mapping, tablename):
        for n, t in mapping.fields:
            if isinstance(t, TrigramIndex):
                cur.execute("""
                    CREATE INDEX "%(tablename)s_trgm_idx_%(column)s" ON "%(tablename)s" USING GIST ("%(column)s" gist_trgm_ops)
                """ % dict(tablename=tablename, column=n))
            if isinstance(t, (StringIndex, Index)):
                cur.execute("""
                    CREATE INDEX "%(tablename)s_idx_%(column)s" ON "%(tablename)s" ("%(column)s")
                """ % dict(tablename=tablename, column=n))

    def swap_tables(self, new_prefix, existing_prefix, backup_prefix):
        cur = self.connection.cursor()

        # remove views before tables, because remove_tables will also remove
        # views via CASCADE and we need the view names for cleanup of
        # geometry_columns
        self.remove_views(backup_prefix)
        self.remove_tables(backup_prefix)

        cur.execute('SELECT tablename FROM pg_tables WHERE tablename like %s', (existing_prefix + '%', ))
        existing_tables = []
        for row in cur:
            table_name = row[0]
            if table_name.startswith(existing_prefix) and not table_name.startswith((new_prefix, backup_prefix)):
                # check for overlapping prefixes: osm_ but not osm_new_ or osm_backup_
                existing_tables.append(table_name)

        cur.execute('SELECT viewname FROM pg_views WHERE viewname like %s', (existing_prefix + '%', ))
        existing_views = []
        for row in cur:
            view_name = row[0]
            if view_name.startswith(existing_prefix) and not view_name.startswith((new_prefix, backup_prefix)):
                # check for overlapping prefixes: osm_ but not osm_new_ or osm_backup_
                existing_views.append(view_name)

        cur.execute('SELECT indexname FROM pg_indexes WHERE indexname like %s', (existing_prefix + '%', ))
        existing_indexes = set()
        for row in cur:
            index_name = row[0]
            if index_name.startswith(existing_prefix) and not index_name.startswith((new_prefix, backup_prefix)):
                # check for overlapping prefixes: osm_ but not osm_new_ or osm_backup_
                existing_indexes.add(index_name)

        cur.execute('SELECT relname FROM pg_class WHERE relname like %s', (existing_prefix + '%_id_seq', ))
        existing_seq = set()
        for row in cur:
            seq_name = row[0]
            if seq_name.startswith(existing_prefix) and not seq_name.startswith((new_prefix, backup_prefix)):
                # check for overlapping prefixes: osm_ but not osm_new_ or osm_backup_
                existing_seq.add(seq_name)

        cur.execute('SELECT tablename FROM pg_tables WHERE tablename like %s', (new_prefix + '%', ))
        new_tables = []
        for row in cur:
            table_name = row[0]
            new_tables.append(table_name)

        cur.execute('SELECT viewname FROM pg_views WHERE viewname like %s', (new_prefix + '%', ))
        new_views = []
        for row in cur:
            view_name = row[0]
            new_views.append(view_name)

        cur.execute('SELECT indexname FROM pg_indexes WHERE indexname like %s', (new_prefix + '%', ))
        new_indexes = set()
        for row in cur:
            index_name = row[0]
            new_indexes.add(index_name)

        cur.execute('SELECT relname FROM pg_class WHERE relname like %s', (new_prefix + '%_id_seq', ))
        new_seq = []
        for row in cur:
            seq_name = row[0]
            new_seq.append(seq_name)


        if not new_tables:
            raise RuntimeError('did not found tables to swap')

        # rename existing tables (osm_) to backup_prefix (osm_backup_)
        for table_name in existing_tables:
            rename_to = table_name.replace(existing_prefix, backup_prefix)
            cur.execute('ALTER TABLE "%s" RENAME TO "%s"' % (table_name, rename_to))

            for idx in existing_indexes:
                if idx in (table_name + '_geom', table_name + '_pkey') or idx.startswith(table_name + '_trgm_idx_') or idx.startswith(table_name + '_idx_'):
                    new_idx = idx.replace(table_name, rename_to, 1)
                    cur.execute('ALTER INDEX "%s" RENAME TO "%s"' % (idx, new_idx))
            if table_name + '_id_seq' in existing_seq:
                cur.execute('ALTER SEQUENCE "%s" RENAME TO "%s"' % (table_name + '_id_seq', rename_to + '_id_seq'))
            if self.use_geometry_columns_table:
                cur.execute('UPDATE geometry_columns SET f_table_name = %s WHERE f_table_name = %s', (rename_to, table_name))

        # rename existing views (osm_) to backup_prefix (osm_backup_)
        for view_name in existing_views:
            rename_to = view_name.replace(existing_prefix, backup_prefix)
            cur.execute('ALTER VIEW "%s" RENAME TO "%s"' % (view_name, rename_to))

            if self.use_geometry_columns_table:
                cur.execute('UPDATE geometry_columns SET f_table_name = %s WHERE f_table_name = %s', (rename_to, view_name))

        # rename new tables (osm_new_) to existing_prefix (osm_)
        for table_name in new_tables:
            rename_to = table_name.replace(new_prefix, existing_prefix)
            cur.execute('ALTER TABLE "%s" RENAME TO "%s"' % (table_name, rename_to))

            for idx in new_indexes:
                if idx in (table_name + '_geom', table_name + '_pkey') or idx.startswith(table_name + '_trgm_idx_') or idx.startswith(table_name + '_idx_'):
                    new_idx = idx.replace(table_name, rename_to, 1)
                    cur.execute('ALTER INDEX "%s" RENAME TO "%s"' % (idx, new_idx))
            if table_name + '_id_seq' in new_seq:
                cur.execute('ALTER SEQUENCE "%s" RENAME TO "%s"' % (table_name + '_id_seq', rename_to + '_id_seq'))
            if self.use_geometry_columns_table:
                cur.execute('UPDATE geometry_columns SET f_table_name = %s WHERE f_table_name = %s', (rename_to, table_name))

        # rename new views (osm_new_) to existing_prefix (osm_)
        for view_name in new_views:
            rename_to = view_name.replace(new_prefix, existing_prefix)
            cur.execute('ALTER VIEW "%s" RENAME TO "%s"' % (view_name, rename_to))

            if self.use_geometry_columns_table:
                cur.execute('UPDATE geometry_columns SET f_table_name = %s WHERE f_table_name = %s', (rename_to, view_name))


    def remove_tables(self, prefix):
        cur = self.connection.cursor()
        cur.execute('SELECT tablename FROM pg_tables WHERE tablename like %s', (prefix + '%', ))
        remove_tables = [row[0] for row in cur]

        for table_name in remove_tables:
            cur.execute("DROP TABLE %s CASCADE" % (table_name, ))
            if self.use_geometry_columns_table:
                cur.execute("DELETE FROM geometry_columns WHERE f_table_name = %s", (table_name, ))


    def remove_views(self, prefix):
        cur = self.connection.cursor()
        cur.execute('SELECT viewname FROM pg_views WHERE viewname like %s', (prefix + '%', ))
        remove_views = [row[0] for row in cur]

        for view_name in remove_views:
            cur.execute('DROP VIEW "%s" CASCADE' % (view_name, ))
            if self.use_geometry_columns_table:
                cur.execute("DELETE FROM geometry_columns WHERE f_table_name = %s", (view_name, ))


    def create_views(self, mappings, ignore_errors=False):
        for mapping in mappings.values():
            if isinstance(mapping, UnionView):
                PostGISUnionView(self, mapping).create(ignore_errors=ignore_errors)

    def create_generalized_tables(self, mappings):
        mappings = [m for m in mappings.values() if isinstance(m, GeneralizedTable)]
        for mapping in sorted(mappings, key=lambda x: x.name, reverse=True):
            PostGISGeneralizedTable(self, mapping).create()

    def postprocess_tables(self, mappings):
        mappings = [m for m in mappings.values() if isinstance(m, FixInvalidPolygons)]
        for mapping in mappings:
            PostGISFixInvalidPolygons(self, mapping).update()

    def optimize(self, mappings):
        mappings = [m for m in mappings.values() if isinstance(m, (GeneralizedTable, Mapping))]
        for mapping in mappings:
            table_name = self.to_tablename(mapping.name)
            self.optimize_table(table_name, '%s_geom' % table_name)
        self.vacuum()

    def optimize_table(self, table_name, idx_name):
        cur = self.connection.cursor()
        print 'Clustering table %s' % table_name
        cur.execute('CLUSTER "%s" ON "%s"' % (idx_name, table_name))
        self.connection.commit()

    def vacuum(self):
        old_isolation_level = self.connection.isolation_level
        self.reconnect()
        self.connection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cur = self.connection.cursor()
        print 'Vacuum analyze'
        cur.execute("VACUUM ANALYZE")
        self.connection.set_isolation_level(old_isolation_level)

class PostGISUnionView(object):
    def __init__(self, db, mapping):
        self.mapping = mapping
        self.db = db
        self.view_name = db.to_tablename(mapping.name)

    def _view_stmt(self):
        selects = []
        if config.imposm_pg_serial_id:
            serial_column = "id, "
        else:
            serial_column = ""

        for mapping in self.mapping.mappings:
            field_str = ', '.join(self._mapping_fields(mapping))
            selects.append("""SELECT %s osm_id, geometry, %s,
                '%s' as class from "%s" """ % (
                serial_column, field_str,
                mapping.classname or mapping.name, self.db.to_tablename(mapping.name)))

        selects = '\nUNION ALL\n'.join(selects)

        stmt = 'CREATE VIEW "%s" as (\n%s\n)' % (self.view_name, selects)

        return stmt

    def _geom_table_stmt(self):
        assert self.db.use_geometry_columns_table
        stmt = "insert into geometry_columns values ('', 'public', '%s', 'geometry', 2, %d, 'GEOMETRY')" % (
            self.view_name, self.db.srid)
        return stmt

    def _mapping_fields(self, mapping):
        mapping_fields = set([n for n, t in mapping.fields])
        fields = []
        for name, default in self.mapping.fields:
            if name in mapping_fields:
                fields.append('"' + name + '"')
            else:
                if default is None:
                    default = 'null'
                elif isinstance(default, basestring):
                    default = "'%s'" % default
                else:
                    default = str(default)
                fields.append(default + ' as "' + name + '"')
        return fields

    def create(self, ignore_errors):
        cur = self.db.connection.cursor()
        cur.execute('BEGIN')

        self.db.drop_table_or_view(cur, self.view_name)

        with self.db.savepoint(cur, raise_errors=not ignore_errors):
            cur.execute(self._view_stmt())

        if self.db.use_geometry_columns_table:
            cur.execute('SELECT * FROM geometry_columns WHERE f_table_name = %s', (self.view_name, ))
            if cur.fetchall():
                # drop old entry to handle changes of SRID
                cur.execute('DELETE FROM geometry_columns WHERE f_table_name = %s', (self.view_name, ))
            cur.execute(self._geom_table_stmt())


class PostGISGeneralizedTable(object):
    def __init__(self, db, mapping):
        self.db = db
        self.mapping = mapping
        self.table_name = db.to_tablename(mapping.name)

    def _geom_table_stmt(self):
        assert self.db.use_geometry_columns_table
        stmt = "insert into geometry_columns values ('', 'public', '%s', 'geometry', 2, %d, 'GEOMETRY')" % (
            self.table_name, self.db.srid)
        return stmt

    def _stmt(self):
        fields = ', '.join(['"' + n + '"' for n, t in self.mapping.fields])
        if fields:
            fields += ','

        where = ''
        if self.mapping.where:
            where = ' WHERE %s' % (self.mapping.where)

        if config.imposm_pg_serial_id:
            serial_column = "id, "
        else:
            serial_column = ""

        return """CREATE TABLE "%s" AS (SELECT %s osm_id, %s
            ST_SimplifyPreserveTopology(geometry, %f) as geometry from "%s"%s)""" % (
            self.table_name, serial_column, fields, self.mapping.tolerance,
            self.db.to_tablename(self.mapping.origin.name),
            where)

    def create(self):
        cur = self.db.connection.cursor()
        cur.execute('BEGIN')

        self.db.drop_table_or_view(cur, self.table_name)

        cur.execute(self._stmt())

        if self.db.use_geometry_columns_table:
            cur.execute('SELECT * FROM geometry_columns WHERE f_table_name = %s', (self.table_name, ))
            if cur.fetchall():
                # drop old entry to handle changes of SRID
                cur.execute('DELETE FROM geometry_columns WHERE f_table_name = %s', (self.table_name, ))
            cur.execute(self._geom_table_stmt())

class PostGISFixInvalidPolygons(object):
    """
    Try to make all polygons valid.
    ST_SimplifyPreserveTopology (used for the generalized tables) can return invalid
    geometries but ST_Buffer should be able to fix them.
    """
    def __init__(self, db, mapping):
        self.db = db
        self.mapping = mapping
        self.table_name = db.to_tablename(mapping.name)

    def _fetch_invalid_geometries(self):
        select_invalid = 'SELECT osm_id FROM %s WHERE ST_IsValid(geometry)=False' %(self.table_name,)

        cur = self.db.connection.cursor()
        cur.execute(select_invalid)

        for row in cur:
            yield row[0]

    def update(self):
        if self.mapping.geom_type != 'GEOMETRY':
            log.info('Validating of polygons only usable for Polygon/GEOMETRY mappings')
            return

        cur = self.db.connection.cursor()

        # fix geometries one-by-one because ST_buffer can fail an we wouldn't be able to
        # tell wich geometry caused it to fail
        for osm_id in self._fetch_invalid_geometries():
            update = 'UPDATE %s SET geometry = ST_Buffer(geometry,0) WHERE osm_id = %d' % (self.table_name, osm_id)
            cur.execute('SAVEPOINT polygonfix;')
            try:
                cur.execute(update)
            except psycopg2.DatabaseError, ex:
                log.warn('Could not fix geometry with osm_id %d. Row will be deleted. Internal error was: %s' % (osm_id, ex))
                cur.execute('ROLLBACK TO SAVEPOINT polygonfix;')
                cur.execute('DELETE FROM %s WHERE osm_id = %d' % (self.table_name, osm_id))
            else:
                cur.execute('RELEASE SAVEPOINT polygonfix;')

class TrigramIndex(object):
    pass

class StringIndex(object):
    pass

class Index(object):
    pass
