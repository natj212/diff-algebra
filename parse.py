import HTMLParser
from BeautifulSoup import BeautifulSoup, SoupStrainer
from mo_files import File
from mo_times import Timer
    '\\': np.array([0, 0], dtype=int),  # FOR "\ no newline at end of file"
def parse_changeset_to_matrix(branch, changeset_id, new_source_code=None):
    unescape = HTMLParser.HTMLParser().unescape
    with Timer("parsing http diff"):
        doc = BeautifulSoup(
            response.content,
            parseOnlyThese=SoupStrainer("pre", {"class": "sourcelines"})
        )
    changeset = "".join(unescape(unicode(l)).rstrip("\r") for l in doc.findAll(text=True))
    :param changeset: THE DIFF TEXT CONTENT
def changeset_to_json(branch, changeset_id, new_source_code=None):
    diff = _get_changeset(branch, changeset_id)
    File("tests/resources/big.patch").write(diff)
    return diff_to_json(diff)


def diff_to_json(changeset):
    """
    CONVERT DIFF TO EASY-TO-STORE JSON FORMAT
    :param changeset: text
    :return: JSON details
    """

    output = []
    files = FILE_SEP.split(changeset)[1:]
    for file_ in files:
        changes = []
        old_file_header, new_file_header, file_diff = file_.split("\n", 2)
        old_file_path = old_file_header[1:]  # eg old_file_header == "a/testing/marionette/harness/marionette_harness/tests/unit/unit-tests.ini"
        new_file_path = new_file_header[5:]  # eg new_file_header == "+++ b/tests/resources/example_file.py"

        coord = []
        c = np.array([0,0], dtype=int)
        hunks = HUNK_SEP.split(file_diff)[1:]
        for hunk in hunks:
            line_diffs = hunk.split("\n")
            old_start, old_length, new_start, new_length = HUNK_HEADER.match(line_diffs[0]).groups()
            next_c = np.array([max(0, int(new_start)-1), max(0, int(old_start)-1)], dtype=int)
            if next_c[0] - next_c[1] != c[0] - c[1]:
                Log.error("expecting a skew of {{skew}}", skew=next_c[0] - next_c[1])
            if c[0] > next_c[0]:
                Log.error("can not handle out-of-order diffs")
            while c[0] != next_c[0]:
                coord.append(copy(c))
                c += no_change

            for line in line_diffs[1:]:
                if not line:
                    continue
                if line.startswith("new file mode")or line.startswith("deleted file mode"):
                    # HAPPENS AT THE TOP OF NEW FILES
                    # u'new file mode 100644'
                    # u'deleted file mode 100644'
                    break
                d = line[0]
                if d == '+':
                    changes.append({"new": {"line": int(c[0]), "content": line[1:]}})
                elif d == '-':
                    changes.append({"old": {"line": int(c[1]), "content": line[1:]}})
                try:
                    c += MOVE[d]
                except Exception as e:
                    Log.warning("bad line {{line|quote}}", line=line, cause=e)

        output.append({
            "new": {"name": new_file_path},
            "old": {"name": old_file_path},
            "changes": changes
        })
    return output







