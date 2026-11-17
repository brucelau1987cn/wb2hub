"""Both desktop navigation variants must link to the maintained repository."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))

class RepositoryLinksTest(unittest.TestCase):
    def test_repository_navigation_links_target_maintenance_repo(self):
        parser = Links()
        parser.feed((Path(__file__).resolve().parents[1] / 'dashboard.html').read_text())
        links = [a for a in parser.links if a.get('class') == 'gh-link' or
                 ('next-side-link' in a.get('class', '') and 'github.com/' in a.get('href', ''))]
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(link['href'], 'https://github.com/brucelau1987cn/wb2hub')
            self.assertIn('noopener', link.get('rel', ''))

if __name__ == '__main__':
    unittest.main()
