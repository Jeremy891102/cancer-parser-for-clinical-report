import json
import unittest
from pathlib import Path
import run_note_pilot as app

class StructuredNoteTests(unittest.TestCase):
    def test_strict_payload_and_legacy_mode(self):
        schema={'type':'object','properties':{},'required':[],'additionalProperties':False}
        args=('gpt-6-luna/v1.0.0','rules',{'metadata':{},'full_text':'test'},3000,'low')
        payload=app.make_payload(*args, schema=schema)
        self.assertEqual(payload['response_format'],{'type':'json_schema','json_schema':{'name':'note_output','strict':True,'schema':schema}})
        self.assertEqual(app.make_payload(*args)['response_format'],{'type':'json_object'})
        self.assertEqual(payload['messages'][1]['content'],json.dumps(args[2],ensure_ascii=False))

    def test_schema_matches_output_contract_and_prompt_preserves_rules(self):
        root=Path(app.ROOT)
        schema=json.loads((root/'schemas/note_structured_schema.json').read_text())
        def check_object(s,keys):
            self.assertEqual(set(s['properties']),set(keys))
            self.assertEqual(set(s['required']),set(keys))
            self.assertIs(s['additionalProperties'],False)
        check_object(schema,app.TEMPLATE)
        check_object(schema['properties']['diagnoses']['items'],app.TEMPLATE['diagnoses'][0])
        self.assertEqual(schema['properties']['derived_classification']['enum'],app.CATEGORIES)
        old=(root/'prompts/note_v1.3.txt').read_text()
        new=(root/'prompts/note_v1.4_structured.txt').read_text()
        self.assertEqual(old[old.index('## 3. Metadata'):],new[new.index('## 3. Metadata'):])
        self.assertNotIn('"mrn": null',new)
