from __future__ import absolute_import, division, print_function

from datetime import datetime
from urlparse import urlparse

from changes.utils.cache import memoize
from changes.utils.http import build_uri

from .base import Vcs, RevisionResult, BufferParser, CommandError, UnknownRevision

LOG_FORMAT = '%H\x01%an <%ae>\x01%at\x01%cn <%ce>\x01%ct\x01%P\x01%B\x02'

ORIGIN_PREFIX = 'remotes/origin/'

BASH_CLONE_STEP = """
#!/bin/bash -eux

REMOTE_URL=%(remote_url)s
LOCAL_PATH=%(local_path)s
REVISION=%(revision)s

if [ ! -d $LOCAL_PATH/.git ]; then
    GIT_SSH_COMMAND="ssh -v" \
    git clone $REMOTE_URL $LOCAL_PATH || \
    git clone $REMOTE_URL $LOCAL_PATH
    pushd $LOCAL_PATH
else
    pushd $LOCAL_PATH
    git remote set-url origin $REMOTE_URL
    GIT_SSH_COMMAND="ssh -v" \
    git fetch --all -p || \
    git fetch --all -p
fi

git clean -fdx

if ! git reset --hard $REVISION ; then
    echo "Failed to update to $REVISION"
    exit 1
fi
""".strip()

BASH_PATCH_STEP = """
#!/bin/bash -eux

LOCAL_PATH=%(local_path)s
PATCH_URL=%(patch_url)s

pushd $LOCAL_PATH
PATCH_PATH=/tmp/$(mktemp patch.XXXXXXXXXX)
curl -o $PATCH_PATH $PATCH_URL
git apply $PATCH_PATH
""".strip()


class LazyGitRevisionResult(RevisionResult):
    def __init__(self, vcs, *args, **kwargs):
        self.vcs = vcs
        super(LazyGitRevisionResult, self).__init__(*args, **kwargs)

    @memoize
    def branches(self):
        return self.vcs.branches_for_commit(self.id)


class GitVcs(Vcs):
    binary_path = 'git'

    def get_default_env(self):
        return {
            'GIT_SSH': self.ssh_connect_path,
        }

    # This is static so that the repository serializer can easily use it
    @staticmethod
    def get_default_revision():
        return 'master'

    @property
    def remote_url(self):
        if self.url.startswith(('ssh:', 'http:', 'https:')):
            parsed = urlparse(self.url)
            url = '%s://%s@%s/%s' % (
                parsed.scheme,
                parsed.username or self.username or 'git',
                parsed.hostname + (':%s' % (parsed.port,) if parsed.port else ''),
                parsed.path.lstrip('/'),
            )
        else:
            url = self.url
        return url

    def branches_for_commit(self, _id):
        return self.get_known_branches(commit_id=_id)

    def get_known_branches(self, commit_id=None):
        """ List all branches or those related to the commit for this repo.

        Either gets all the branches (if the commit_id is not specified) or then
        the branches related to the given commit reference.

        :param commit_id: A commit ID for fetching all related branches. If not
            specified, returns all branch names for this repository.
        :return: List of branches for the commit, or all branches for the repo.
        """
        results = []
        command_parameters = ['branch', '-a']
        if commit_id:
            command_parameters.extend(['--contains', commit_id])
        output = self.run(command_parameters)

        for result in output.splitlines():
            # HACK(dcramer): is there a better way around removing the prefix?
            result = result[2:].strip()
            if result.startswith(ORIGIN_PREFIX):
                result = result[len(ORIGIN_PREFIX):]
            if result == 'HEAD':
                continue
            results.append(result)
        return list(set(results))

    def run(self, cmd, **kwargs):
        cmd = [self.binary_path] + cmd
        try:
            return super(GitVcs, self).run(cmd, **kwargs)
        except CommandError as e:
            if 'unknown revision or path' in e.stderr:
                raise UnknownRevision(
                    cmd=e.cmd,
                    retcode=e.retcode,
                    stdout=e.stdout,
                    stderr=e.stderr,
                )
            raise

    def clone(self):
        self.run(['clone', '--mirror', self.remote_url, self.path])

    def update(self):
        self.run(['remote', 'set-url', 'origin', self.remote_url])
        self.run(['fetch', '--all', '-p'])

    def log(self, parent=None, branch=None, author=None, offset=0, limit=100):
        """ Gets the commit log for the repository.

        Each revision returned includes all the branches with which this commit
        is associated. There will always be at least one associated branch.

        See documentation for the base for general information on this function.
        """
        # TODO(dcramer): we should make this streaming
        cmd = ['log', '--date-order', '--pretty=format:%s' % (LOG_FORMAT,)]

        if author:
            cmd.append('--author=%s' % (author,))
        if offset:
            cmd.append('--skip=%d' % (offset,))
        if limit:
            cmd.append('--max-count=%d' % (limit,))

        if parent and branch:
            raise ValueError('Both parent and branch cannot be set')
        if branch:
            cmd.append(branch)

        # TODO(dcramer): determine correct way to paginate results in git as
        # combining --all with --parent causes issues
        elif not parent:
            cmd.append('--all')
        if parent:
            cmd.append(parent)

        try:
            result = self.run(cmd)
        except CommandError as cmd_error:
            err_msg = cmd_error.stderr
            if branch and branch in err_msg:
                import traceback
                import logging
                msg = traceback.format_exception(CommandError, cmd_error, None)
                logging.warning(msg)
                raise ValueError('Unable to fetch commit log for branch "{0}".'
                                 .format(branch))
            raise

        for chunk in BufferParser(result, '\x02'):
            (sha, author, author_date, committer, committer_date,
             parents, message) = chunk.split('\x01')

            # sha may have a trailing newline due to git log adding it
            sha = sha.lstrip('\n')

            parents = filter(bool, parents.split(' '))

            author_date = datetime.utcfromtimestamp(float(author_date))
            committer_date = datetime.utcfromtimestamp(float(committer_date))

            yield LazyGitRevisionResult(
                vcs=self,
                id=sha,
                author=author,
                committer=committer,
                author_date=author_date,
                committer_date=committer_date,
                parents=parents,
                message=message,
            )

    def export(self, id):
        """Get the textual diff for a revision.
        Args:
            id (str): The id of the revision.
        Returns:
            A string with the text of the diff for the revision.
        Raises:
            UnknownRevision: If the revision wasn't found.
        """
        cmd = ['diff', '%s^..%s' % (id, id)]
        result = self.run(cmd)
        return result

    def is_child_parent(self, child_in_question, parent_in_question):
        cmd = ['merge-base', '--is-ancestor', parent_in_question, child_in_question]
        try:
            self.run(cmd)
            return True
        except CommandError:
            return False

    def get_buildstep_clone(self, source, workspace):
        return BASH_CLONE_STEP % dict(
            remote_url=self.remote_url,
            local_path=workspace,
            revision=source.revision_sha,
        )

    def get_buildstep_patch(self, source, workspace):
        return BASH_PATCH_STEP % dict(
            local_path=workspace,
            patch_url=build_uri('/api/0/patches/{0}/?raw=1'.format(
                                source.patch_id.hex)),
        )
