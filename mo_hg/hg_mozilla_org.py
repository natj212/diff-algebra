# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import division
from __future__ import unicode_literals

import re
from copy import copy

import mo_threads
from BeautifulSoup import BeautifulSoup
from future.utils import text_type, binary_type
from jx_python import jx
from mo_dots import set_default, Null, coalesce, unwraplist, Data
from mo_files import File
from mo_kwargs import override
from mo_logs import Log, strings, machine_metadata
from mo_logs.exceptions import Explanation, assert_no_exception, Except
from mo_logs.strings import expand_template
from mo_math.randoms import Random
from mo_threads import Thread, Lock, Queue, THREAD_STOP, Signal
from mo_threads import Till
from mo_times import Timer
from mo_times.dates import Date
from mo_times.durations import SECOND, Duration, HOUR
from pyLibrary import convert
from pyLibrary.env import http, elasticsearch
from pyLibrary.meta import cache

from mo_hg.parse import diff_to_json
from mo_hg.repos.changesets import Changeset
from mo_hg.repos.pushs import Push
from mo_hg.repos.revisions import Revision, revision_schema

_hg_branches = None
_OLD_BRANCH = None


def _late_imports():
    global _hg_branches
    global _OLD_BRANCH

    from mo_hg import hg_branches as _hg_branches
    from mo_hg.hg_branches import OLD_BRANCH as _OLD_BRANCH

    _ = _hg_branches
    _ = _OLD_BRANCH


DEFAULT_LOCALE = "en-US"
DEBUG = True

GET_DIFF = "{{location}}/raw-rev/{{rev}}"
GET_FILE = "{{location}}/raw-file/{{rev}}{{path}}"


last_called_url = {}


class HgMozillaOrg(object):
    """
    USE hg.mozilla.org FOR REPO INFORMATION
    USE ES AS A FASTER CACHE FOR THE SAME
    """

    @override
    def __init__(
        self,
        hg=None,        # CONNECT TO hg
        repo=None,      # CONNECTION INFO FOR ES CACHE
        branches=None,  # CONNECTION INFO FOR ES CACHE
        use_cache=False,   # True IF WE WILL USE THE ES FOR DOWNLOADING BRANCHES
        timeout=30 * SECOND,
        kwargs=None
    ):
        if not _hg_branches:
            _late_imports()

        self.es_locker = Lock()
        self.todo = mo_threads.Queue("todo")

        self.settings = kwargs
        self.timeout = Duration(timeout)

        if branches == None:
            self.branches = _hg_branches.get_branches(kwargs=kwargs)
            self.es = None
            return

        set_default(repo, {"schema": revision_schema})
        self.es = elasticsearch.Cluster(kwargs=repo).get_or_create_index(kwargs=repo)
        self.es.add_alias()
        try:
            self.es.set_refresh_interval(seconds=1)
        except Exception:
            pass

        self.branches = _hg_branches.get_branches(kwargs=kwargs)

        Thread.run("hg daemon", self._daemon)

    def _daemon(self, please_stop):
        while not please_stop:
            with Explanation("getting extra revision"):
                r = self.todo.pop(till=please_stop)
                output = Data(signal=Signal(), revision=None)
                self._load_all_in_push(output, r, locale=None, please_stop=please_stop)

    @cache(duration=HOUR, lock=True)
    def get_revision(self, revision, locale=None):
        """
        EXPECTING INCOMPLETE revision OBJECT
        RETURNS revision
        """
        rev = revision.changeset.id
        if not rev:
            return Null
        elif rev == "None":
            return Null
        elif revision.branch.name == None:
            return Null
        locale = coalesce(locale, revision.branch.locale, DEFAULT_LOCALE)
        doc = self._get_from_elasticsearch(revision, locale=locale)
        if doc:
            Log.note("Got hg ({{branch}}, {{locale}}, {{revision}}) from ES", branch=doc.branch.name, locale=locale, revision=doc.changeset.id)
            return doc

        found_revision = copy(revision)
        if isinstance(found_revision.branch, (text_type, binary_type)):
            lower_name = found_revision.branch.lower()
        else:
            lower_name = found_revision.branch.name.lower()

        if not lower_name:
            Log.error("Defective revision? {{rev|json}}", rev=found_revision.branch)

        b = found_revision.branch = self.branches[(lower_name, locale)]
        if not b:
            b = found_revision.branch = self.branches[(lower_name, DEFAULT_LOCALE)]
            if not b:
                Log.error("can not find branch ({{branch}}, {{locale}})", branch=lower_name, locale=locale)

        if Date.now() - Date(b.etl.timestamp) > _OLD_BRANCH:
            self.branches = _hg_branches.get_branches(kwargs=self.settings)

        pushes = self._get_pushlog(found_revision.branch, found_revision.changeset.id)
        if len(pushes) != 1:
            Log.error("do not know what to do")
        push = pushes[0]

        url = found_revision.branch.url.rstrip("/") + "/json-info?node=" + found_revision.changeset.id[0:12]
        raw_revs = self._get_revision(url, found_revision.branch)
        if len(raw_revs) != 1:
            Log.error("do not know what to do")
        output = self._normalize_revision(raw_revs.values()[0], found_revision, push)
        return output

    def _get_from_elasticsearch(self, revision, locale=None):
        rev = revision.changeset.id
        query = {
            "query": {"filtered": {
                "query": {"match_all": {}},
                "filter": {"and": [
                    {"prefix": {"changeset.id": rev[0:12]}},
                    {"term": {"branch.name": revision.branch.name}},
                    {"term": {"branch.locale": coalesce(locale, revision.branch.locale, DEFAULT_LOCALE)}}
                ]}
            }},
            "size": 2000,
        }

        for attempt in range(3):
            try:
                docs = self.es.search(query).hits.hits
                break
            except Exception as e:
                e = Except.wrap(e)
                if "NodeNotConnectedException" in e:
                    # WE LOST A NODE, THIS MAY TAKE A WHILE
                    (Till(seconds=Random.int(5 * 60))).wait()
                    continue
                elif "EsRejectedExecutionException[rejected execution (queue capacity" in e:
                    (Till(seconds=Random.int(30))).wait()
                    continue
                else:
                    Log.warning("Bad ES call, fall back to TH", cause=e)
                    return None

        best = docs[0]._source
        if len(docs) > 1:
            for d in docs:
                if d._id.endswith(d._source.branch.locale):
                    best = d._source
            Log.warning("expecting no more than one document")

        if best.changeset.diff:
            return best
        else:
            return None

    @cache(duration=HOUR, lock=True)
    def _get_revision(self, url, branch):
        return self._get_and_retry(url, branch)

    @cache(duration=HOUR, lock=True)
    def _get_pushlog(self, branch, changeset_id):
        url = branch.url.rstrip("/") + "/json-pushes?full=1&changeset=" + changeset_id
        Log.note(
            "Reading pushlog for revision {{changeset}}): {{url}}",
            urlh=branch.url,
            changeset=changeset_id

        )
        with Explanation("Pulling pushlog from {{url}}", url=url):
            data = self._get_and_retry(url, branch)
            return [
                Push(id=int(index), date=_push.date, user=_push.user)
                for index, _push in data.items()
            ]

    def _normalize_revision(self, r, found_revision, push):
        rev = Revision(
            branch=found_revision.branch,
            index=r.rev,
            changeset=Changeset(
                id=r.node,
                id12=r.node[0:12],
                author=r.user,
                description=strings.limit(r.description, 2000),
                date=Date(r.date),
                files=r.files,
                backedoutby=r.backedoutby
            ),
            parents=unwraplist(r.parents),
            children=unwraplist(r.children),
            push=push,
            etl={"timestamp": Date.now().unix, "machine": machine_metadata}
        )
        # ADD THE DIFF
        diff = self._get_unified_diff_from_hg(rev)
        rev.changeset.diff = diff_to_json(diff)

        self.todo.extend(rev.changeset.parents)
        self.todo.extend(rev.changeset.children)

        _id = coalesce(rev.changeset.id12, "") + "-" + rev.branch.name + "-" + coalesce(rev.branch.locale, DEFAULT_LOCALE)
        with self.es_locker:
            self.es.add({"id": _id, "value": rev})

        return rev

    def _get_and_retry(self, url, branch, **kwargs):
        """
        requests 2.5.0 HTTPS IS A LITTLE UNSTABLE
        """
        kwargs = set_default(kwargs, {"timeout": self.timeout.seconds})
        try:
            return _get_url(url, branch, **kwargs)
        except Exception as e:
            pass

        try:
            (Till(seconds=5)).wait()
            return _get_url(url.replace("https://", "http://"), branch, **kwargs)
        except Exception as f:
            pass

        path = url.split("/")
        if path[3] == "l10n-central":
            # FROM https://hg.mozilla.org/l10n-central/tr/json-pushes?full=1&changeset=a6eeb28458fd
            # TO   https://hg.mozilla.org/mozilla-central/json-pushes?full=1&changeset=a6eeb28458fd
            path = path[0:3] + ["mozilla-central"] + path[5:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 5 and path[5] == "mozilla-aurora":
            # FROM https://hg.mozilla.org/releases/l10n/mozilla-aurora/pt-PT/json-pushes?full=1&changeset=b44a8c68fc60
            # TO   https://hg.mozilla.org/releases/mozilla-aurora/json-pushes?full=1&changeset=b44a8c68fc60
            path = path[0:4] + ["mozilla-aurora"] + path[7:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 5 and path[5] == "mozilla-beta":
            # FROM https://hg.mozilla.org/releases/l10n/mozilla-beta/lt/json-pushes?full=1&changeset=03fbf7556c94
            # TO   https://hg.mozilla.org/releases/mozilla-beta/json-pushes?full=1&changeset=b44a8c68fc60
            path = path[0:4] + ["mozilla-beta"] + path[7:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 7 and path[5] == "mozilla-release":
            # FROM https://hg.mozilla.org/releases/l10n/mozilla-release/en-GB/json-pushes?full=1&changeset=57f513ab03308adc7aa02cc2ea8d73fe56ae644b
            # TO   https://hg.mozilla.org/releases/mozilla-release/json-pushes?full=1&changeset=57f513ab03308adc7aa02cc2ea8d73fe56ae644b
            path = path[0:4] + ["mozilla-release"] + path[7:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 5 and path[4] == "autoland":
            # FROM https://hg.mozilla.org/build/autoland/json-pushes?full=1&changeset=3ccccf8e5036179a3178437cabc154b5e04b333d
            # TO  https://hg.mozilla.org/integration/autoland/json-pushes?full=1&changeset=3ccccf8e5036179a3178437cabc154b5e04b333d
            path = path[0:3] + ["try"] + path[5:]
            return self._get_and_retry("/".join(path), branch, **kwargs)

        Log.error("Tried {{url}} twice.  Both failed.", {"url": url}, cause=[e, f])

    @cache(duration=HOUR, lock=True)
    def find_changeset(self, revision, please_stop=False):
        locker = Lock()
        output = []
        queue = Queue("branches", max=2000)
        queue.extend(self.branches)
        queue.add(THREAD_STOP)

        problems = []
        def _find(please_stop):
            for b in queue:
                if please_stop:
                    return
                try:
                    url = b.url + "json-info?node=" + revision
                    response = http.get(url, timeout=30)
                    if response.status_code == 200:
                        with locker:
                            output.append(b)
                        Log.note("{{revision}} found at {{url}}", url=url, revision=revision)
                except Exception as f:
                    problems.append(f)

        threads = []
        for i in range(20):
            threads.append(Thread.run("find changeset " + text_type(i), _find, please_stop=please_stop))

        for t in threads:
            with assert_no_exception:
                t.join()

        if problems:
            Log.error("Could not scan for {{revision}}", revision=revision, cause=problems[0])

        return output

    def _extract_bug_id(self, description):
        """
        LOOK INTO description to FIND bug_id
        """
        if description == None:
            return None
        match = re.findall(r'[Bb](?:ug)?\s*([0-9]{5,7})', description)
        if match:
            return int(match[0])
        return None

    def _get_unified_diff_from_hg(self, revision):
        """
        :param revision: INOMPLETE REVISION OBJECT 
        :return: 
        """
        response = http.get(expand_template(GET_DIFF, {"location": revision.branch.url, "rev": revision.changeset.id}))
        try:
            return response.content.decode("utf8", "replace")
        except Exception as e:
            Log.error("can not decode", cause=e)

    def _get_source_code_from_hg(self, revision, file_path):
        response = http.get(expand_template(GET_FILE, {"location": revision.branch.url, "rev": revision.changeset.id, "path": file_path}))
        return response.content.decode("utf8", "replace")





def _trim(url):
    return url.split("/json-pushes?")[0].split("/json-info?")[0]


def _get_url(url, branch, **kwargs):
    with Explanation("get push from {{url}}", url=url):
        response = http.get(url, **kwargs)
        data = convert.json2value(response.content.decode("utf8"))
        if isinstance(data, (text_type, str)) and data.startswith("unknown revision"):
            Log.error("Unknown push {{revision}}", revision=strings.between(data, "'", "'"))
        branch.url = _trim(url)  #RECORD THIS SUCCESS IN THE BRANCH
        return data
