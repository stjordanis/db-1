import relstorage.adapters.postgresql
import relstorage.adapters.postgresql.mover
import relstorage.adapters.postgresql.schema

from .jsonpickle import Jsonifier
from ._util import trigger_exists

class Adapter(relstorage.adapters.postgresql.PostgreSQLAdapter):

    def __init__(self, *args, **kw):
        super(Adapter, self).__init__(*args, **kw)

        driver = relstorage.adapters.postgresql.select_driver(self.options)
        self.schema = SchemaInstaller(
            connmanager=self.connmanager,
            runner=self.runner,
            locker=self.locker,
            keep_history=self.keep_history,
        )
        self.mover = Mover(
            database_type='postgresql',
            options=self.options,
            runner=self.runner,
            version_detector=self.version_detector,
            Binary=driver.Binary,
        )
        self.connmanager.set_on_store_opened(self.mover.on_store_opened)

        self.mover.jsonifier = Jsonifier(
            transform=getattr(self.options, 'transform', None))
        self.mover.auxiliary_tables = getattr(self.options,
                                              'auxiliary_tables', ())

class Mover(relstorage.adapters.postgresql.mover.PostgreSQLObjectMover):

    def on_store_opened(self, cursor, restart=False):
        cursor.execute("""\
        select from information_schema.tables
        where table_name = 'temp_store' and table_type = 'LOCAL TEMPORARY'
        """)
        if not list(cursor):
            super(Mover, self).on_store_opened(cursor, restart)

        cursor.execute("""
            CREATE TEMPORARY TABLE IF NOT EXISTS temp_store_json (
                zoid         BIGINT NOT NULL,
                class_name   TEXT,
                ghost_pickle BYTEA,
                state        JSONB
            ) ON COMMIT DROP""")

    def store_temp(self, cursor, batcher, oid, prev_tid, data):
        super(Mover, self).store_temp(cursor, batcher, oid, prev_tid, data)
        class_name, ghost_pickle, state = self.jsonifier(oid, data)
        if class_name is None:
            return
        batcher.delete_from('temp_store_json', zoid=oid)
        batcher.insert_into(
            "temp_store_json (zoid, class_name, ghost_pickle, state)",
            "%s, %s, %s, %s",
            (oid, class_name, self.Binary(ghost_pickle), state),
            rowkey=oid,
            size=len(state),
            )

    _move_json_sql = """
    LOCK TABLE newt IN SHARE MODE;

    INSERT INTO newt (zoid, class_name, ghost_pickle, state)
    SELECT zoid, class_name, ghost_pickle, state
    FROM temp_store_json order by zoid
    ON CONFLICT (zoid) DO UPDATE SET
      class_name = EXCLUDED.class_name,
      ghost_pickle = EXCLUDED.ghost_pickle,
      state = EXCLUDED.state;
    """

    _update_aux_sql = """
    DELETE FROM %(name)s WHERE zoid IN (SELECT zoid FROM temp_store);
    INSERT INTO %(name)s (zoid)
    SELECT zoid FROM temp_store join newt using (zoid);
    """

    def move_from_temp(self, cursor, tid, txn_has_blobs):
        r = super(Mover, self).move_from_temp(cursor, tid, txn_has_blobs)
        cursor.execute(self._move_json_sql)
        for name in self.auxiliary_tables:
            cursor.execute(self._update_aux_sql % dict(name=name))
        return r

    def restore(self, cursor, batcher, oid, tid, data):
        super(Mover, self).restore(cursor, batcher, oid, tid, data)
        class_name, ghost_pickle, state = self.jsonifier(oid, data)
        if class_name is None:
            return
        batcher.delete_from('newt', zoid=oid)
        batcher.insert_into(
            "newt (zoid, class_name, ghost_pickle, state)",
            "%s, %s, %s, %s",
            (oid, class_name, self.Binary(ghost_pickle), state),
            rowkey=oid,
            size=len(state),
            )

_newt_delete_on_state_delete = """
create function newt_delete_on_state_delete() returns trigger
as $$
begin
  delete from newt where zoid = OLD.zoid;
  return old;
end;
$$ language plpgsql;
"""

_newt_delete_on_state_delete_HP = """
create function newt_delete_on_state_delete() returns trigger
as $$
declare
  current_tid bigint;
begin
  select tid from current_object where zoid = OLD.zoid into current_tid;
  if current_tid is null or current_tid = OLD.tid then
    delete from newt where zoid = OLD.zoid;
  end if;
  return OLD;
end;
$$ language plpgsql;
"""

_newt_ddl = """\
create table newt (
  zoid bigint primary key,
  class_name text,
  ghost_pickle bytea,
  state jsonb);
create index newt_json_idx on newt using gin (state);
"""

DELETE_TRIGGER = 'newt_delete_on_state_delete_trigger'

def _create_newt_delete_trigger(cursor, keep_history):
    cursor.execute(
        _newt_delete_on_state_delete_HP if keep_history else
        _newt_delete_on_state_delete)
    cursor.execute("""
    create trigger %s
      after delete on object_state for each row
      execute procedure newt_delete_on_state_delete();
    """ % DELETE_TRIGGER)

def create_newt(cursor, keep_history=None):
    keep_history = determine_keep_history(cursor, keep_history)
    cursor.execute(_newt_ddl)
    _create_newt_delete_trigger(cursor, keep_history)

class SchemaInstaller(
    relstorage.adapters.postgresql.schema.PostgreSQLSchemaInstaller):

    def create(self, cursor):
        super(SchemaInstaller, self).create(cursor)
        create_newt(cursor, self.keep_history)

    def update_schema(self, cursor, tables):
        if 'newt' not in tables:
            create_newt(cursor)
        if not trigger_exists(cursor, DELETE_TRIGGER):
            _create_newt_delete_trigger(cursor, self.keep_history)

    def drop_all(self):
        def callback(_conn, cursor):
            cursor.execute("drop table if exists newt")
            cursor.execute(
                "drop function if exists newt_delete_on_state_delete() cascade"
                )
        self.connmanager.open_and_call(callback)
        super(SchemaInstaller, self).drop_all()

def determine_keep_history(cursor, keep_history=None):
    """Determine whether the RelStorage databases is set to keep history.
    """
    if keep_history is None:
        # We don't know, so sniff
        cursor.execute(
            "select 1 from pg_catalog.pg_class "
            "where relname = 'current_object'")
        keep_history = bool(list(cursor))

    return keep_history
