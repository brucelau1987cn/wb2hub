"""Runtime annotation compatibility for the standalone session module."""
import ast
from pathlib import Path
import unittest


class SessionPythonCompatibility(unittest.TestCase):
    def test_import_does_not_evaluate_pep604_union_on_python39(self):
        source = (Path(__file__).resolve().parents[1] / 'wb_session.py').read_text()
        tree = ast.parse(source, feature_version=(3, 9))
        postponed = any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
                        and any(a.name == 'annotations' for a in n.names) for n in tree.body)
        unions = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp)
                  and isinstance(n.op, ast.BitOr)]
        self.assertTrue(postponed or not unions, 'Python 3.9 cannot evaluate PEP604 annotations')


if __name__ == '__main__':
    unittest.main()
