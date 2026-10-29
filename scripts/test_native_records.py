import json
import tempfile
import unittest
from pathlib import Path
from native_records import collect_round, resolve_transcript

SID = '80d20633-9f7f-4e35-b2d8-3c9028106850'

class NativeRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / (SID + '.jsonl')
        self.cwd = str(self.root)

    def save(self, rows):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf8')

    def omp(self, ident, parent, role, text='', stop=None):
        return {'type':'message','id':ident,'parentId':parent,'message':{'role':role,'content':[{'type':'text','text':text}], 'stopReason':stop}}

    def claude(self, ident, parent, role, content, stop=None, side=False, mid=None):
        return {'type':role,'uuid':ident,'parentUuid':parent,'cwd':self.cwd,'sessionId':SID,'isSidechain':side,
                'message':{'role':role,'id':mid or ident,'content':content,'stop_reason':stop}}

    def collect(self, kind):
        return collect_round(kind,self.path,self.cwd,'job-1','request.md')

    def test_omp_rounds_and_tools(self):
        rows=[{'type':'session','id':'session','cwd':self.cwd},self.omp('u',None,'user','job-1 request.md'),
              self.omp('a','u','assistant','intermediate','toolUse'),self.omp('t','a','toolResult'),
              self.omp('f','t','assistant','OK','stop')]
        self.save(rows)
        self.assertEqual(self.collect('omp')['text'],'OK')
        rows += [self.omp('u2','f','user','other request'),self.omp('f2','u2','assistant','old','stop')]
        self.save(rows); self.assertIsNone(self.collect('omp'))

    def test_abnormal_and_partial_tail(self):
        base=[{'type':'session','id':'session','cwd':self.cwd},self.omp('u',None,'user','job-1 request.md'),self.omp('a','u','assistant','intermediate','stop')]
        for stop in ('toolUse','error','aborted','length',None):
            self.save(base+[self.omp('b','a','assistant','not final',stop)])
            self.assertIsNone(self.collect('omp'))
        self.save(base)
        with self.path.open('a',encoding='utf8') as f: f.write('{')
        self.assertIsNone(self.collect('omp'))

    def test_branch_and_sidechain(self):
        txt=lambda s:[{'type':'text','text':s}]
        rows=[self.claude('u',None,'user','job-1 request.md'),self.claude('a','u','assistant',txt('correct'),'end_turn'),
              self.claude('side','u','assistant',txt('wrong'),'end_turn',True)]
        self.save(rows); self.assertEqual(self.collect('claude')['text'],'correct')
        rows.append(self.claude('branch','u','assistant',txt('branch'),'end_turn'))
        self.save(rows); self.assertEqual(self.collect('claude')['text'],'branch')

    def test_claude_tool_result_and_stop(self):
        txt=lambda s:[{'type':'text','text':s}]
        rows=[self.claude('u',None,'user','job-1 request.md'),self.claude('a','u','assistant',[{'type':'tool_use','id':'t'}],'tool_use'),
              self.claude('t','a','user',[{'type':'tool_result','tool_use_id':'t','content':'result'}]),
              self.claude('f','t','assistant',txt('Y'),'end_turn')]
        self.save(rows); self.assertEqual(self.collect('claude')['text'],'Y')
        rows[-1]['message']['stop_reason']=None
        self.save(rows); self.assertEqual(self.collect('claude')['finality'],'idle-tail-text')
        for reason in ('max_tokens','refusal','error'):
            rows[-1]['message']['stop_reason']=reason; self.save(rows); self.assertIsNone(self.collect('claude'))
        rows[-1]['message']['stop_reason']='end_turn'; rows[-1]['isApiErrorMessage']=True
        self.save(rows); self.assertIsNone(self.collect('claude'))

    def test_identity_and_resolution(self):
        rows=[self.claude('u',None,'user','job-1 request.md'),self.claude('a','u','assistant',[{'type':'text','text':'yes'}],'stop_sequence')]
        self.save(rows)
        self.assertEqual(resolve_transcript('claude',{'kind':'path','value':str(self.path)},self.cwd),self.path)
        with self.assertRaises(ValueError): resolve_transcript('claude',{'kind':'path','value':str(self.path)},self.root/'other')
        projects=self.root/'cfg'/'projects'; (projects/'one').mkdir(parents=True)
        target=projects/'one'/self.path.name; target.write_bytes(self.path.read_bytes())
        self.assertEqual(resolve_transcript('claude',{'kind':'id','value':SID},self.cwd,{'CLAUDE_CONFIG_DIR':str(self.root/'cfg')}),target)
        (projects/'two').mkdir(); (projects/'two'/self.path.name).write_bytes(self.path.read_bytes())
        with self.assertRaises(ValueError): resolve_transcript('claude',{'kind':'id','value':SID},self.cwd,{'CLAUDE_CONFIG_DIR':str(self.root/'cfg')})
        with self.assertRaises(ValueError): resolve_transcript('claude',{'kind':'id','value':'../bad'},self.cwd)
        self.assertIsNone(collect_round('claude',self.path,self.cwd,'nope','request.md'))

    def test_duplicate_message_id_is_not_concatenated(self):
        rows=[self.claude('u',None,'user','job-1 request.md'), self.claude('a','u','assistant',[{'type':'text','text':'one'}],None,mid='same'),self.claude('b','a','assistant',[{'type':'text','text':'two'}],'end_turn',mid='same')]
        self.save(rows); self.assertIsNone(self.collect('claude'))

    def test_claude_native_split_final_and_isolates_rounds(self):
        # Observed native shape: an assistant's non-text block and text block
        # share an ID, each reporting end_turn, followed by a duration record.
        # Synthetic content deliberately contains no hidden reasoning.
        txt=lambda s:[{'type':'text','text':s}]
        rows=[self.claude('u',None,'user','job-1 request.md'),
              self.claude('a','u','assistant',[{'type':'thinking'}],'end_turn',mid='first'),
              self.claude('b','a','assistant',txt('first final'),'end_turn',mid='first'),
              {'type':'system','subtype':'turn_duration','uuid':'d','parentUuid':'b','isSidechain':False},
              self.claude('u2','d','user','job-1 next.md'),
              self.claude('a2','u2','assistant',[{'type':'thinking'}],'end_turn',mid='second'),
              self.claude('b2','a2','assistant',txt('second final'),'end_turn',mid='second'),
              {'type':'system','subtype':'turn_duration','uuid':'d2','parentUuid':'b2','isSidechain':False}]
        self.save(rows)
        self.assertIsNone(self.collect('claude'))
        self.assertEqual(collect_round('claude',self.path,self.cwd,'job-1','next.md')['text'], 'second final')
        self.save(rows[:4]); self.assertEqual(self.collect('claude')['text'], 'first final')

    def test_claude_split_final_rejects_ambiguous_or_abnormal_records(self):
        import copy
        base=[self.claude('u',None,'user','job-1 request.md'),
              self.claude('a','u','assistant',[{'type':'thinking'}],'end_turn',mid='same'),
              self.claude('b','a','assistant',[{'type':'text','text':'final'}],'end_turn',mid='same')]
        cases=[]
        for reason in (None, 'max_tokens', 'error'):
            rows=copy.deepcopy(base); rows[1]['message']['stop_reason']=reason; cases.append(rows)
            rows=copy.deepcopy(base); rows[2]['message']['stop_reason']=reason; cases.append(rows)
        for block in ({'type':'text','text':'earlier'}, {'type':'tool_use'}, {'type':'unknown'}):
            rows=copy.deepcopy(base); rows[1]['message']['content']=[block]; cases.append(rows)
        rows=copy.deepcopy(base); rows[1]['message']['content']=[]; cases.append(rows)
        rows=copy.deepcopy(base); rows[2]['message']['content'].append({'type':'text','text':'extra'}); cases.append(rows)
        for key in ('isApiErrorMessage', 'isError'):
            rows=copy.deepcopy(base); rows[1][key]=True; cases.append(rows)
        for key in ('error', 'isError', 'refusal'):
            rows=copy.deepcopy(base); rows[1]['message'][key]=True; cases.append(rows)
        for rows in cases:
            with self.subTest(rows=rows):
                self.save(rows); self.assertIsNone(self.collect('claude'))
        self.save(base)
        with self.path.open('a',encoding='utf8') as f: f.write('{')
        self.assertIsNone(self.collect('claude'))

    def test_claude_split_final_requires_adjacency_and_consistent_metadata(self):
        import copy
        base=[self.claude('u',None,'user','job-1 request.md'),
              self.claude('a','u','assistant',[{'type':'thinking'}],'end_turn',mid='same'),
              self.claude('b','a','assistant',[{'type':'text','text':'final'}],'end_turn',mid='same')]
        for key, nested in (('requestId', False), ('model', True)):
            rows=copy.deepcopy(base)
            left=rows[1]['message'] if nested else rows[1]
            right=rows[2]['message'] if nested else rows[2]
            left[key]='one'; right[key]='two'
            self.save(rows); self.assertIsNone(self.collect('claude'))
            right[key]='one'
            self.save(rows); self.assertEqual(self.collect('claude')['text'], 'final')
        for middle in (self.claude('x','a','assistant',[{'type':'text','text':'intermediate'}],'end_turn',mid='other'),
                       self.claude('x','a','user',[{'type':'tool_result','tool_use_id':'t'}])):
            rows=copy.deepcopy(base); rows[2]['parentUuid']='x'; rows.insert(2,middle)
            self.save(rows); self.assertIsNone(self.collect('claude'))

if __name__ == '__main__': unittest.main()
