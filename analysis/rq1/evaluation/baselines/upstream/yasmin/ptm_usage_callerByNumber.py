import json
from datetime import time
from typing import List, Any

import astor
import pandas as pd
import logging
import os
from ExtractSource import ExtractSource
import traceback

from nltk.tokenize import word_tokenize


def fetch(filename, line):
    with open(filename, 'r', encoding="utf8") as src_file:
        try:
            source = src_file.read()
            if len(source):
                es = ExtractSource(source)
                es.set_blob(filename)
                es.get_ast()
                es.get_func_defs()
                caller_name = es.fetch_caller(line)
        except Exception as e:
            print("exception: " + filename + " " + str(e))
            logging.error('exception at: ' + filename + " " + str(e))
            traceback.print_exc()

    return caller_name

