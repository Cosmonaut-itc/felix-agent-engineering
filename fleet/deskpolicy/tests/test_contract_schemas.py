"""The contract names every schema the installed runtime emits or persists."""
import re
import unittest
from pathlib import Path

from deskpolicy.store import SOURCE_FILES

MODULE = Path(__file__).resolve().parents[1]
SCHEMA = re.compile(r'pao/desk-[a-z-]+@[0-9]+')


class ContractSchemaTest(unittest.TestCase):
    def test_contract_names_every_runtime_schema(self):
        contract = set(SCHEMA.findall((MODULE.parent / 'contracts/desk-politica-v1.md').read_text()))
        runtime = {schema for name in SOURCE_FILES for schema in SCHEMA.findall((MODULE / name).read_text())}
        self.assertIn('pao/desk-policy@1', runtime)
        self.assertEqual(sorted(runtime - contract), [])
