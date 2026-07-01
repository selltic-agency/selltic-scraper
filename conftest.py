import os
import sys

# Repo root na sys.path, żeby `import scraper_core` działało z tests/ bez instalacji pakietu.
sys.path.insert(0, os.path.dirname(__file__))
