import copy
import json
import tempfile
import unittest
from pathlib import Path
from native_records import collect_round, resolve_transcript

SID = '80d20633-9f7f-4e35-b2d8-3c9028106850'

class CodexRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / ('rollout-2026-10-28T00-00-00-' + SID + '.jsonl')
        self.rows = [
            {'type':'session_meta','payload':{'id':SID,'cwd':str(self.root)}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'t'}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'job-1 request.md'}]}},
            {'type':'turn_context','payload':{'turn_id':'t','cwd':str(self.root),'model':'model','effort':'high'}},
            {'type':'response_item','payload':{'type':'reasoning'}},
            {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','id':'m','content':[{'type':'output_text','text':'answer'}]}},
            {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t','last_agent_message':'answer','duration_ms':20,'time_to_first_token_ms':3}},
        ]
    def save(self, rows): self.path.write_text(''.join(json.dumps(x)+'\n' for x in rows), encoding='utf8')
    def collect(self): return collect_round('codex',self.path,self.root,'job-1','request.md')
    def test_success_and_metadata(self):
        self.save(self.rows); r=self.collect(); self.assertEqual(r['text'],'answer'); self.assertEqual(r['message_id'],'m'); self.assertEqual(r['model_observed'],'model'); self.assertEqual(r['effort_observed'],'high'); self.assertEqual(r['duration_ms'],20); self.assertEqual(r['ttft_ms'],3)
    def test_resolution_identity_and_ambiguity(self):
        self.save(self.rows)
        self.assertEqual(resolve_transcript('codex',{'kind':'path','value':str(self.path)},self.root),self.path)
        with self.assertRaises(ValueError): resolve_transcript('codex',{'kind':'path','value':str(self.path)},self.root/'wrong')
        cfg=self.root/'config'; folder=cfg/'sessions'/'2026'/'10'/'28'; folder.mkdir(parents=True); target=folder/self.path.name; target.write_bytes(self.path.read_bytes())
        self.assertEqual(resolve_transcript('codex',{'kind':'id','value':SID},self.root,{'CODEX_HOME':str(cfg)}),target)
        (folder/('rollout-other-'+SID+'.jsonl')).write_bytes(self.path.read_bytes())
        with self.assertRaises(ValueError): resolve_transcript('codex',{'kind':'id','value':SID},self.root,{'CODEX_HOME':str(cfg)})
        rows=copy.deepcopy(self.rows); rows[0]['payload']['id']='11111111-1111-1111-1111-111111111111'; self.save(rows); self.assertIsNone(self.collect())

    def test_archived_id_resolves_and_collects_without_resuming(self):
        self.save(self.rows)
        home = self.root / 'config'
        archive = home / 'archived_sessions'
        archive.mkdir(parents=True)
        target = archive / self.path.name
        self.path.replace(target)
        found = resolve_transcript('codex', {'kind': 'id', 'value': SID}, self.root, {'CODEX_HOME': str(home)})
        self.assertEqual(found, target)
        self.assertEqual(collect_round('codex', found, self.root, 'job-1', 'request.md')['text'], 'answer')
        active = home / 'sessions/2026/10/29' / target.name
        active.parent.mkdir(parents=True)
        active.write_bytes(target.read_bytes())
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            resolve_transcript('codex', {'kind': 'id', 'value': SID}, self.root, {'CODEX_HOME': str(home)})
    def test_rejects_incomplete_error_and_ambiguous_final(self):
        cases=[]
        cases.append(self.rows[:-1]); r=copy.deepcopy(self.rows); r[-1]['payload']['turn_id']='other'; cases.append(r)
        r=copy.deepcopy(self.rows); r[-1]['payload']['last_agent_message']='different'; cases.append(r)
        r=copy.deepcopy(self.rows); r[-2]['payload']['phase']='commentary'; cases.append(r)
        r=copy.deepcopy(self.rows); r[-2]['payload']['content']=[{'type':'tool_output','text':'answer'}]; cases.append(r)
        r=copy.deepcopy(self.rows); r.insert(-1,copy.deepcopy(r[-2])); cases.append(r)
        r=copy.deepcopy(self.rows); r.insert(-1,{'type':'event_msg','payload':{'type':'turn_aborted'}}); cases.append(r)
        r=copy.deepcopy(self.rows); r.insert(-1,{'type':'response_item','payload':{'type':'function_call_output'}}); cases.append(r)
        r=copy.deepcopy(self.rows); r[3]['payload']['cwd']=str(self.root/'other'); cases.append(r)
        for r in cases:
            self.save(r); self.assertIsNone(self.collect())
        self.save(self.rows)
        with self.path.open('a',encoding='utf8') as f: f.write('{')
        self.assertIsNone(self.collect())
    def test_completion_tail_allows_only_known_metadata(self):
        self.save(self.rows + [{'type':'event_msg','payload':{'type':'token_count','info':{}}}])
        self.assertEqual(self.collect()['text'], 'answer')
        tails = [
            {'type':'response_item','payload':{'type':'function_call_output'}},
            {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'later'}]}},
            {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'later'}]}},
            {'type':'event_msg','payload':{'type':'user_message'}},
            {'type':'event_msg','payload':{'type':'item_completed'}},
            {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t'}},
            {'type':'event_msg','payload':{'type':'unknown'}},
            {'type':'unknown','payload':{}},
        ]
        for tail in tails:
            with self.subTest(tail=tail):
                self.save(self.rows + [tail]); self.assertIsNone(self.collect())

    def test_latest_turn_and_latest_user(self):
        self.save(self.rows+[{'type':'event_msg','payload':{'type':'task_started','turn_id':'new'}}]); self.assertIsNone(self.collect())
        r=copy.deepcopy(self.rows); r.insert(4,{'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'other'}]}}); self.save(r); self.assertIsNone(self.collect())
    def test_requires_request_and_complete_header(self):
        self.save(self.rows); self.assertIsNone(collect_round('codex',self.path,self.root,'job-1','other.md'))
        r=copy.deepcopy(self.rows); r.insert(1,copy.deepcopy(r[0])); self.save(r); self.assertIsNone(self.collect())

if __name__ == '__main__': unittest.main()
