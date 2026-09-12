from contextlib import closing
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recover', Path(__file__).resolve().parents[1]/'recover.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base/'home'
        self.home.mkdir()
        self.project = self.base/'project'
        self.project.mkdir()
        (self.project/'README.md').write_text('fixture', encoding='utf-8')
        self.host = 'local:' + str(self.home)
        self.state = {'local-projects':{}, 'project-order':[],
                      'app-server-project-id-by-legacy-project-id-by-host':{self.host:{}},
                      'unrelated':{'dataFlow':1,'dataflow':2},
                      'thread-project-assignments':{'sample-task':{'projectId':'unrelated-project'}}}
        self.write_state()
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as c:
            c.executescript('''
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,created_at_ms INTEGER,updated_at_ms INTEGER);
                CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT);
                CREATE TABLE project_idempotency_keys(key TEXT,project_id TEXT);
                CREATE TABLE threads(id TEXT,cwd TEXT,project_id TEXT);
                INSERT INTO threads VALUES('sample-task','unchanged','unrelated-project');
            ''')
            c.commit()
        self.add_project()

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self):
        (self.home/'.codex-global-state.json').write_text(json.dumps(self.state), encoding='utf-8')

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as c:
            result = c.execute(statement, params).fetchall()
            c.commit()
            return result

    def add_project(self, backend='server-a', key='legacy-a', root=None):
        self.sql('INSERT INTO projects VALUES(?,?,?,?)', (backend,'Example',100,200))
        self.sql('INSERT INTO project_roots VALUES(?,?,?)', (backend,0,str(root or self.project)))
        self.sql('INSERT INTO project_idempotency_keys VALUES(?,?)', (key,backend))

    def plan(self):
        return r.build_plan(self.home, ['legacy-a'])

    def test_normalize_extended_drive_and_case(self):
        self.assertEqual(r.normalize('\\\\?\\C:\\Work\\Demo\\'), r.normalize('c:/work/demo'))

    def test_normalize_unc(self):
        self.assertEqual(r.normalize('\\\\?\\UNC\\server\\share\\demo'), r.normalize('\\\\SERVER\\share\\demo'))

    def test_valid_plan_uses_real_ids(self):
        plan = self.plan()
        self.assertEqual(plan['selected'][0]['backend_id'], 'server-a')
        self.assertEqual(r.validate_plan(plan), self.home)

    def test_deleted_backend_alias_is_not_candidate(self):
        self.sql('DELETE FROM projects')
        _, rows, deleted = r.candidates(self.home, self.state)
        self.assertFalse(rows)
        self.assertEqual(deleted[0]['key'], 'legacy-a')
        with self.assertRaises(ValueError):
            self.plan()

    def test_stale_plan_aborts(self):
        plan = self.plan()
        self.sql('DELETE FROM projects')
        with self.assertRaises(ValueError):
            r.validate_plan(plan)

    def test_edited_plan_aborts(self):
        plan = self.plan()
        plan['selected'][0]['record']['rootPaths'] = [str(self.home)]
        with self.assertRaises(ValueError):
            r.validate_plan(plan)

    def test_root_change_aborts(self):
        plan = self.plan()
        self.sql('UPDATE project_roots SET path=?', (str(self.base),))
        with self.assertRaises(ValueError):
            r.validate_plan(plan)

    def test_cloud_id_not_auto_restored(self):
        self.sql('UPDATE project_idempotency_keys SET key=?', ('g-p-example',))
        rows = r.candidates(self.home, self.state)[1]
        self.assertIn('cloud_linked_needs_separate_review', rows[0]['reasons'])

    def test_cloud_directory_alias_not_auto_restored(self):
        root = self.home/'.chatgpt-projects'/'g-p-example'
        root.mkdir(parents=True)
        (root/'file').touch()
        self.sql('UPDATE project_roots SET path=?', (str(root),))
        with self.assertRaises(ValueError):
            self.plan()

    def test_worktree_path_not_auto_restored(self):
        root = self.home/'.codex'/'worktrees'/'sample'
        root.mkdir(parents=True)
        (root/'file').touch()
        self.sql('UPDATE project_roots SET path=?', (str(root),))
        with self.assertRaises(ValueError):
            self.plan()

    def test_linked_worktree_detected_outside_codex(self):
        gitdir = self.base/'git-admin'
        gitdir.mkdir()
        (gitdir/'commondir').write_text('../main', encoding='utf-8')
        (self.project/'.git').write_text('gitdir: '+str(gitdir), encoding='utf-8')
        self.assertEqual(r.path_kind(str(self.project)), 'worktree')

    def test_empty_auxiliary_root_preserved(self):
        empty = self.base/'empty'
        empty.mkdir()
        self.sql('INSERT INTO project_roots VALUES(?,?,?)', ('server-a',1,str(empty)))
        self.assertEqual(len(self.plan()['selected'][0]['record']['rootPaths']), 2)

    def test_all_empty_roots_blocked(self):
        (self.project/'README.md').unlink()
        with self.assertRaises(ValueError):
            self.plan()

    def test_multiple_aliases_blocked(self):
        self.sql('INSERT INTO project_idempotency_keys VALUES(?,?)', ('another','server-a'))
        with self.assertRaises(ValueError):
            self.plan()

    def test_same_root_selections_blocked(self):
        self.add_project('server-b','legacy-b')
        with self.assertRaises(ValueError):
            r.build_plan(self.home, ['legacy-a','legacy-b'])

    def test_existing_root_not_duplicated(self):
        self.state['local-projects']['existing'] = {'id':'existing','rootPaths':[str(self.project)]}
        self.write_state()
        with self.assertRaises(ValueError):
            self.plan()

    def test_conflicting_mapping_blocked(self):
        self.state['app-server-project-id-by-legacy-project-id-by-host'][self.host]['legacy-a'] = 'different'
        self.write_state()
        with self.assertRaises(ValueError):
            self.plan()

    def test_unrelated_fields_preserved(self):
        result = r.transform(self.state, self.plan())
        self.assertEqual(result['unrelated'], self.state['unrelated'])
        self.assertEqual(result['thread-project-assignments'], self.state['thread-project-assignments'])

    def test_repeat_transform_rejected(self):
        plan = self.plan()
        result = r.transform(self.state, plan)
        with self.assertRaises(ValueError):
            r.transform(result, plan)

    def test_unknown_schema_rejected(self):
        self.sql('DROP TABLE projects')
        with self.assertRaises(ValueError):
            self.plan()

    def test_output_cannot_overwrite_client_state(self):
        with self.assertRaises(ValueError):
            r.safe_output(self.home/'.codex-global-state.json', self.home)

    def test_backup_and_atomic_apply_preserve_database(self):
        plan = self.plan()
        raw = (self.home/'.codex-global-state.json').read_bytes()
        db_before = (self.home/'state_5.sqlite').read_bytes()
        with patch.object(r, 'require_closed'):
            result = r.apply_plan(plan, self.base/'backups')
        self.assertEqual(result['count_after'],1)
        self.assertEqual((Path(result['backup'])/'.codex-global-state.json').read_bytes(), raw)
        self.assertEqual((self.home/'state_5.sqlite').read_bytes(), db_before)
        self.assertEqual(self.sql('SELECT * FROM threads'), [('sample-task','unchanged','unrelated-project')])

    def test_running_client_stops_before_backup(self):
        with patch.object(r, 'require_closed', side_effect=ValueError('running')):
            with self.assertRaises(ValueError):
                r.apply_plan(self.plan(), self.base/'backups')
        self.assertFalse((self.base/'backups').exists())

    def test_atomic_replace_failure_keeps_original(self):
        target = self.base/'sample.json'
        target.write_text('{"old":true}', encoding='utf-8')
        with patch.object(r.os, 'replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                r.atomic_json(target, {'new':True})
        self.assertEqual(target.read_text(), '{"old":true}')

    def test_sessions_only_extract_metadata(self):
        folder = self.home/'sessions'
        folder.mkdir()
        rows = [dict(type='session_meta',payload={'cwd':str(self.project)}),
                dict(type='response_item',payload={'cwd':'C:\\not-metadata'})]
        (folder/'sample.jsonl').write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        found = r.session_paths(self.home)
        self.assertEqual(found['path_counts'], {r.normalize(str(self.project)):1})


if __name__ == '__main__':
    unittest.main()
