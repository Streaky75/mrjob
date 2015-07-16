# -*- coding: utf-8 -*-
# Copyright 2009-2013 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for EMRJobRunner"""
from contextlib import contextmanager
from contextlib import nested
import copy
from datetime import datetime
from datetime import timedelta
import getpass
import logging
import os
import os.path
import posixpath
import shutil
from StringIO import StringIO
import tempfile
import time

from mock import patch
from mock import Mock

try:
    import unittest2 as unittest
    unittest  # quiet "redefinition of unused ..." warning from pyflakes
except ImportError:
    import unittest

import mrjob
import mrjob.emr
from mrjob.fs.s3 import S3Filesystem
from mrjob.emr import EMRJobRunner
from mrjob.emr import attempt_to_acquire_lock
from mrjob.emr import filechunkio
from mrjob.emr import _MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH
from mrjob.emr import _yield_all_steps
from mrjob.emr import _lock_acquire_step_1
from mrjob.emr import _lock_acquire_step_2
from mrjob.parse import JOB_NAME_RE
from mrjob.parse import parse_s3_uri
from mrjob.pool import pool_hash_and_name
from mrjob.ssh import SSH_LOG_ROOT
from mrjob.ssh import SSH_PREFIX
from mrjob.util import bash_wrap
from mrjob.util import log_to_stream
from mrjob.util import tar_and_gzip

from tests.mockboto import MockEmrConnection
from tests.mockboto import MockEmrObject
from tests.mockboto import MockIAMConnection
from tests.mockboto import MockS3Connection
from tests.mockboto import add_mock_s3_data
from tests.mockssh import create_mock_ssh_script
from tests.mockssh import mock_ssh_dir
from tests.mockssh import mock_ssh_file
from tests.mr_hadoop_format_job import MRHadoopFormatJob
from tests.mr_jar_and_streaming import MRJarAndStreaming
from tests.mr_just_a_jar import MRJustAJar
from tests.mr_two_step_job import MRTwoStepJob
from tests.mr_word_count import MRWordCount
from tests.quiet import logger_disabled
from tests.quiet import no_handlers_for_logger
from tests.sandbox import mrjob_conf_patcher
from tests.sandbox import patch_fs_s3
from tests.sandbox import SandboxedTestCase

try:
    import boto
    import boto.emr
    import boto.emr.connection
    import boto.exception
    boto  # quiet "redefinition of unused ..." warning from pyflakes
except ImportError:
    boto = None


class FastEMRTestCase(SandboxedTestCase):

    @classmethod
    def setUpClass(cls):
        # we don't care what's in this file, just want mrjob to stop creating
        # and deleting a complicated archive.
        cls.fake_mrjob_tgz_path = tempfile.mkstemp(
            prefix='fake_mrjob_', suffix='.tar.gz')[1]

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.fake_mrjob_tgz_path):
            os.remove(cls.fake_mrjob_tgz_path)

    def setUp(self):
        super(FastEMRTestCase, self).setUp()

        # patch slow things
        def fake_create_mrjob_tar_gz(mocked_self, *args, **kwargs):
            mocked_self._mrjob_tar_gz_path = self.fake_mrjob_tgz_path
            return self.fake_mrjob_tgz_path

        self.simple_patch(EMRJobRunner, '_create_mrjob_tar_gz',
                     fake_create_mrjob_tar_gz, autospec=True)

        self.simple_patch(EMRJobRunner, '_wait_for_s3_eventual_consistency')
        self.simple_patch(EMRJobRunner, '_wait_for_job_flow_termination')
        self.simple_patch(time, 'sleep')

    def simple_patch(self, obj, attr, side_effect=None, autospec=False,
                     return_value=None):
        patcher = patch.object(obj, attr, side_effect=side_effect,
                               autospec=autospec, return_value=return_value)
        patcher.start()
        self.addCleanup(patcher.stop)


class MockEMRAndS3TestCase(FastEMRTestCase):

    def _mock_boto_connect_s3(self, *args, **kwargs):
        kwargs['mock_s3_fs'] = self.mock_s3_fs
        return MockS3Connection(*args, **kwargs)

    def _mock_boto_emr_EmrConnection(self, *args, **kwargs):
        kwargs['mock_s3_fs'] = self.mock_s3_fs
        kwargs['mock_emr_clusters'] = self.mock_emr_clusters
        kwargs['mock_emr_failures'] = self.mock_emr_failures
        kwargs['mock_emr_output'] = self.mock_emr_output
        return MockEmrConnection(*args, **kwargs)

    def _mock_boto_connect_iam(self, *args, **kwargs):
        kwargs['mock_iam_instance_profiles'] = self.mock_iam_instance_profiles
        kwargs['mock_iam_roles'] = self.mock_iam_roles
        kwargs['mock_iam_role_policies'] = self.mock_iam_role_policies
        kwargs['mock_iam_role_attached_policies'] = (
            self.mock_iam_role_attached_policies)
        return MockIAMConnection(*args, **kwargs)

    def setUp(self):
        # patch boto
        self.mock_emr_failures = {}
        self.mock_emr_clusters = {}
        self.mock_emr_output = {}
        self.mock_iam_instance_profiles = {}
        self.mock_iam_role_attached_policies = {}
        self.mock_iam_role_policies = {}
        self.mock_iam_roles = {}
        self.mock_s3_fs = {}

        p_s3 = patch.object(boto, 'connect_s3', self._mock_boto_connect_s3)
        self.addCleanup(p_s3.stop)
        p_s3.start()

        p_iam = patch.object(boto, 'connect_iam', self._mock_boto_connect_iam)
        self.addCleanup(p_iam.stop)
        p_iam.start()

        p_emr = patch.object(
            boto.emr.connection, 'EmrConnection',
            self._mock_boto_emr_EmrConnection)
        self.addCleanup(p_emr.stop)
        p_emr.start()

        super(MockEMRAndS3TestCase, self).setUp()

    def add_mock_s3_data(self, data, time_modified=None):
        """Update self.mock_s3_fs with a map from bucket name
        to key name to data."""
        add_mock_s3_data(self.mock_s3_fs, data, time_modified)

    def prepare_runner_for_ssh(self, runner, num_slaves=0):
        # TODO: Refactor this abomination of a test harness

        # Set up environment variables
        os.environ['MOCK_SSH_VERIFY_KEY_FILE'] = 'true'

        # Create temporary directories and add them to MOCK_SSH_ROOTS
        master_ssh_root = tempfile.mkdtemp(prefix='master_ssh_root.')
        os.environ['MOCK_SSH_ROOTS'] = 'testmaster=%s' % master_ssh_root
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/history')

        if not hasattr(self, 'slave_ssh_roots'):
            self.slave_ssh_roots = []

        self.addCleanup(self.teardown_ssh, master_ssh_root)

        # Make the fake binary
        os.mkdir(os.path.join(master_ssh_root, 'bin'))
        self.ssh_bin = os.path.join(master_ssh_root, 'bin', 'ssh')
        create_mock_ssh_script(self.ssh_bin)

        # Make a fake keyfile so that the 'file exists' requirements are
        # satsified
        self.keyfile_path = os.path.join(master_ssh_root, 'key.pem')
        with open(self.keyfile_path, 'w') as f:
            f.write('I AM DEFINITELY AN SSH KEY FILE')

        # Tell the runner to use the fake binary
        runner._opts['ssh_bin'] = [self.ssh_bin]
        # Inject master node hostname so it doesn't try to 'emr --describe' it
        runner._address = 'testmaster'
        # Also pretend to have an SSH key pair file
        runner._opts['ec2_key_pair_file'] = self.keyfile_path

        # re-initialize fs
        runner._fs = None
        runner._ssh_fs = None
        runner._s3_fs = None
        #runner.fs

    def add_slave(self):
        """Add a mocked slave to the cluster. Caller is responsible for setting
        runner._opts['num_ec2_instances'] to the correct number.
        """
        slave_num = len(self.slave_ssh_roots)
        new_dir = tempfile.mkdtemp(prefix='slave_%d_ssh_root.' % slave_num)
        self.slave_ssh_roots.append(new_dir)
        os.environ['MOCK_SSH_ROOTS'] += (':testmaster!testslave%d=%s'
                                         % (slave_num, new_dir))

    def teardown_ssh(self, master_ssh_root):
        shutil.rmtree(master_ssh_root)
        for path in self.slave_ssh_roots:
            shutil.rmtree(path)

    def make_runner(self, *args):
        """create a dummy job, and call make_runner() on it.
        Use this in a with block:

        with self.make_runner() as runner:
            ...
        """
        stdin = StringIO('foo\nbar\n')
        mr_job = MRTwoStepJob(['-r', 'emr'] + list(args))
        mr_job.sandbox(stdin=stdin)

        return mr_job.make_runner()

    def run_and_get_cluster(self, *args):
        # not sure why we include -v
        with self.make_runner('-v', args) as runner:
            runner.run()
            emr_conn = runner.make_emr_conn()
            return emr_conn.describe_cluster(runner.get_cluster_id())


class EMRJobRunnerEndToEndTestCase(MockEMRAndS3TestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
        'additional_emr_info': {'key': 'value'}
    }}}

    def test_end_to_end(self):
        # read from STDIN, a local file, and a remote file
        stdin = StringIO('foo\nbar\n')

        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as local_input_file:
            local_input_file.write('bar\nqux\n')

        remote_input_path = 's3://walrus/data/foo'
        self.add_mock_s3_data({'walrus': {'data/foo': 'foo\n'}})

        # setup fake output
        self.mock_emr_output = {('j-MOCKCLUSTER0', 1): [
            '1\t"qux"\n2\t"bar"\n', '2\t"foo"\n5\tnull\n']}

        mr_job = MRHadoopFormatJob(['-r', 'emr', '-v',
                                    '-', local_input_path, remote_input_path,
                                    '--jobconf', 'x=y'])
        mr_job.sandbox(stdin=stdin)

        local_tmp_dir = None
        results = []

        mock_s3_fs_snapshot = copy.deepcopy(self.mock_s3_fs)

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)

            # make sure that initializing the runner doesn't affect S3
            # (Issue #50)
            self.assertEqual(mock_s3_fs_snapshot, self.mock_s3_fs)

            # make sure AdditionalInfo was JSON-ified from the config file.
            # checked now because you can't actually read it from the job flow
            # on real EMR.
            self.assertEqual(runner._opts['additional_emr_info'],
                             '{"key": "value"}')
            runner.run()

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

            local_tmp_dir = runner._get_local_tmp_dir()
            # make sure cleanup hasn't happened yet
            self.assertTrue(os.path.exists(local_tmp_dir))
            self.assertTrue(any(runner.ls(runner.get_output_dir())))

            emr_conn = runner.make_emr_conn()
            cluster = emr_conn.describe_cluster(runner.get_cluster_id())
            self.assertEqual(cluster.status.state, 'TERMINATED')
            name_match = JOB_NAME_RE.match(cluster.name)
            self.assertEqual(name_match.group(1), 'mr_hadoop_format_job')
            self.assertEqual(name_match.group(2), getpass.getuser())

            # make sure our input and output formats are attached to
            # the correct steps
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            step_0_args = [arg.value for arg in steps[0].config.args]
            step_1_args = [arg.value for arg in steps[1].config.args]

            self.assertIn('-inputformat', step_0_args)
            self.assertNotIn('-outputformat', step_0_args)
            self.assertNotIn('-inputformat', step_1_args)
            self.assertIn('-outputformat', step_1_args)

            # make sure jobconf got through
            self.assertIn('-D', step_0_args)
            self.assertIn('x=y', step_0_args)
            self.assertIn('-D', step_1_args)
            # job overrides jobconf in step 1
            self.assertIn('x=z', step_1_args)

            # make sure mrjob.tar.gz is created and uploaded as
            # a bootstrap file
            self.assertTrue(os.path.exists(runner._mrjob_tar_gz_path))
            self.assertIn(runner._mrjob_tar_gz_path,
                          runner._upload_mgr.path_to_uri())
            self.assertIn(runner._mrjob_tar_gz_path,
                          runner._bootstrap_dir_mgr.paths())

        self.assertEqual(sorted(results),
                         [(1, 'qux'), (2, 'bar'), (2, 'foo'), (5, None)])

        # make sure cleanup happens
        self.assertFalse(os.path.exists(local_tmp_dir))
        self.assertFalse(any(runner.ls(runner.get_output_dir())))

        # job should get terminated
        emr_conn = runner.make_emr_conn()
        cluster_id = runner.get_cluster_id()
        for _ in xrange(10):
            emr_conn.simulate_progress(cluster_id)

        cluster = emr_conn.describe_cluster(cluster_id)
        self.assertEqual(cluster.status.state, 'TERMINATED')

    def test_failed_job(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '-v'])
        mr_job.sandbox()

        self.add_mock_s3_data({'walrus': {}})
        self.mock_emr_failures = {('j-MOCKCLUSTER0', 0): None}

        with no_handlers_for_logger('mrjob.emr'):
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)

            with mr_job.make_runner() as runner:
                self.assertIsInstance(runner, EMRJobRunner)

                self.assertRaises(Exception, runner.run)
                # make sure job flow ID printed in error string
                self.assertIn('Job on job flow j-MOCKCLUSTER0 failed',
                              stderr.getvalue())

                emr_conn = runner.make_emr_conn()
                cluster_id = runner.get_cluster_id()
                for _ in xrange(10):
                    emr_conn.simulate_progress(cluster_id)

                cluster = emr_conn.describe_cluster(cluster_id)
                self.assertEqual(cluster.status.state,
                                 'TERMINATED_WITH_ERRORS')

            # job should get terminated on cleanup
            cluster_id = runner.get_cluster_id()
            for _ in xrange(10):
                emr_conn.simulate_progress(cluster_id)

        cluster = emr_conn.describe_cluster(runner.get_cluster_id())
        self.assertEqual(cluster.status.state, 'TERMINATED_WITH_ERRORS')

    def _test_remote_scratch_cleanup(self, mode, scratch_len, log_len):
        self.add_mock_s3_data({'walrus': {'logs/j-MOCKCLUSTER0/1': '1\n'}})
        stdin = StringIO('foo\nbar\n')

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--s3-log-uri', 's3://walrus/logs',
                               '-', '--cleanup', mode])
        mr_job.sandbox(stdin=stdin)

        with mr_job.make_runner() as runner:
            s3_scratch_uri = runner._opts['s3_scratch_uri']
            scratch_bucket, _ = parse_s3_uri(s3_scratch_uri)

            runner.run()

            # this is set and unset before we can get at it unless we do this
            log_bucket, _ = parse_s3_uri(runner._s3_job_log_uri)

            list(runner.stream_output())

        conn = runner.make_s3_conn()
        bucket = conn.get_bucket(scratch_bucket)
        self.assertEqual(len(list(bucket.list())), scratch_len)

        bucket = conn.get_bucket(log_bucket)
        self.assertEqual(len(list(bucket.list())), log_len)

    def test_cleanup_all(self):
        self._test_remote_scratch_cleanup('ALL', 0, 0)

    def test_cleanup_scratch(self):
        self._test_remote_scratch_cleanup('SCRATCH', 0, 1)

    def test_cleanup_remote(self):
        self._test_remote_scratch_cleanup('REMOTE_SCRATCH', 0, 1)

    def test_cleanup_local(self):
        self._test_remote_scratch_cleanup('LOCAL_SCRATCH', 5, 1)

    def test_cleanup_logs(self):
        self._test_remote_scratch_cleanup('LOGS', 5, 0)

    def test_cleanup_none(self):
        self._test_remote_scratch_cleanup('NONE', 5, 1)

    def test_cleanup_combine(self):
        self._test_remote_scratch_cleanup('LOGS,REMOTE_SCRATCH', 0, 0)

    def test_cleanup_error(self):
        self.assertRaises(ValueError, self._test_remote_scratch_cleanup,
                          'NONE,LOGS,REMOTE_SCRATCH', 0, 0)
        self.assertRaises(ValueError, self._test_remote_scratch_cleanup,
                          'GARBAGE', 0, 0)

    def test_wait_for_job_flow_termination(self):
        # Test regression from #338 where _wait_for_job_flow_termination
        # would raise an IndexError whenever the job flow wasn't already
        # finished
        mr_job = MRTwoStepJob(['-r', 'emr'])
        mr_job.sandbox()
        with mr_job.make_runner() as runner:
            runner._add_job_files_for_upload()
            runner._launch_emr_job()
            cluster = runner._describe_cluster
            runner._wait_for_job_flow_termination()


class S3ScratchURITestCase(MockEMRAndS3TestCase):

    def test_pick_scratch_uri(self):
        self.add_mock_s3_data({'mrjob-walrus': {}, 'zebra': {}})
        runner = EMRJobRunner(conf_paths=[])

        self.assertEqual(runner._opts['s3_scratch_uri'],
                         's3://mrjob-walrus/tmp/')

    def test_create_scratch_uri(self):
        # "walrus" bucket will be ignored; it doesn't start with "mrjob-"
        self.add_mock_s3_data({'walrus': {}, 'zebra': {}})

        runner = EMRJobRunner(conf_paths=[], s3_sync_wait_time=0.00)

        # bucket name should be mrjob- plus 16 random hex digits
        s3_scratch_uri = runner._opts['s3_scratch_uri']
        self.assertEqual(s3_scratch_uri[:11], 's3://mrjob-')
        self.assertEqual(s3_scratch_uri[27:], '/tmp/')

        # bucket shouldn't actually exist yet
        scratch_bucket, _ = parse_s3_uri(s3_scratch_uri)
        self.assertNotIn(scratch_bucket, self.mock_s3_fs.keys())

        # need to do something to ensure that the bucket actually gets
        # created. let's launch a (mock) job flow
        job_flow_id = runner.make_persistent_job_flow()
        self.assertIn(scratch_bucket, self.mock_s3_fs.keys())
        runner.make_emr_conn().terminate_jobflow(job_flow_id)

        # once our scratch bucket is created, we should re-use it
        runner2 = EMRJobRunner(conf_paths=[])
        s3_scratch_uri = runner._opts['s3_scratch_uri']
        self.assertEqual(runner2._opts['s3_scratch_uri'], s3_scratch_uri)


class ExistingJobFlowTestCase(MockEMRAndS3TestCase):

    def test_attach_to_existing_job_flow(self):
        emr_conn = EMRJobRunner(conf_paths=[]).make_emr_conn()
        # set log_uri to None, so that when we describe the job flow, it
        # won't have the loguri attribute, to test Issue #112
        emr_job_flow_id = emr_conn.run_jobflow(
            name='Development Job Flow', log_uri=None,
            keep_alive=True)

        stdin = StringIO('foo\nbar\n')
        self.mock_emr_output = {(emr_job_flow_id, 1): [
            '1\t"bar"\n1\t"foo"\n2\tnull\n']}

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--emr-job-flow-id', emr_job_flow_id])
        mr_job.sandbox(stdin=stdin)

        results = []
        with mr_job.make_runner() as runner:
            runner.run()

            # Issue 182: don't create the bootstrap script when
            # attaching to another job flow
            self.assertIsNone(runner._master_bootstrap_script_path)

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

        self.assertEqual(sorted(results),
                         [(1, 'bar'), (1, 'foo'), (2, None)])

    def test_dont_take_down_job_flow_on_failure(self):
        emr_conn = EMRJobRunner(conf_paths=[]).make_emr_conn()
        # set log_uri to None, so that when we describe the job flow, it
        # won't have the loguri attribute, to test Issue #112
        emr_job_flow_id = emr_conn.run_jobflow(
            name='Development Job Flow', log_uri=None,
            keep_alive=True)

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--emr-job-flow-id', emr_job_flow_id])
        mr_job.sandbox()

        self.add_mock_s3_data({'walrus': {}})
        self.mock_emr_failures = {('j-MOCKCLUSTER0', 0): None}

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.emr'):
                self.assertRaises(Exception, runner.run)

            emr_conn = runner.make_emr_conn()
            job_flow_id = runner.get_cluster_id()
            for _ in xrange(10):
                emr_conn.simulate_progress(job_flow_id)

            job_flow = emr_conn.describe_jobflow(job_flow_id)
            self.assertEqual(job_flow.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        emr_conn = runner.make_emr_conn()
        job_flow_id = runner.get_cluster_id()
        for _ in xrange(10):
            emr_conn.simulate_progress(job_flow_id)

        job_flow = emr_conn.describe_jobflow(job_flow_id)
        self.assertEqual(job_flow.state, 'WAITING')


class VisibleToAllUsersTestCase(MockEMRAndS3TestCase):

    def test_defaults(self):
        cluster = self.run_and_get_cluster()
        self.assertEqual(job_flow.visibletoallusers, 'false')

    def test_visible(self):
        cluster = self.run_and_get_cluster('--visible-to-all-users')
        self.assertTrue(job_flow.visibletoallusers, 'true')


class IAMTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(IAMTestCase, self).setUp()

        # wrap connect_iam() so we can see if it was called
        p_iam = patch.object(boto, 'connect_iam', wraps=boto.connect_iam)
        self.addCleanup(p_iam.stop)
        p_iam.start()

    def run_and_get_cluster(self, *args):
        stdin = StringIO('foo\nbar\n')
        mr_job = MRTwoStepJob(
            ['-r', 'emr', '-v'] + list(args))
        mr_job.sandbox(stdin=stdin)

        with mr_job.make_runner() as runner:
            runner.run()
            emr_conn = runner.make_emr_conn()
            return emr_conn.describe_jobflow(runner.get_cluster_id())

    def test_role_auto_creation(self):
        cluster = self.run_and_get_cluster()
        self.assertTrue(boto.connect_iam.called)

        # check instance_profile
        instance_profile_name = job_flow.jobflowrole
        self.assertIsNotNone(instance_profile_name)
        self.assertTrue(instance_profile_name.startswith('mrjob-'))
        self.assertIn(instance_profile_name, self.mock_iam_instance_profiles)
        self.assertIn(instance_profile_name, self.mock_iam_roles)
        self.assertIn(instance_profile_name,
                      self.mock_iam_role_attached_policies)

        # check service_role
        service_role_name = job_flow.servicerole
        self.assertIsNotNone(service_role_name)
        self.assertTrue(service_role_name.startswith('mrjob-'))
        self.assertIn(service_role_name, self.mock_iam_roles)
        self.assertIn(service_role_name,
                      self.mock_iam_role_attached_policies)

        # instance_profile and service_role should be distinct
        self.assertNotEqual(instance_profile_name, service_role_name)

        # run again, and see if we reuse the roles
        job_flow2 = self.run_and_get_cluster()

        self.assertEqual(job_flow2.jobflowrole, instance_profile_name)
        self.assertEqual(job_flow2.servicerole, service_role_name)


    def test_iam_instance_profile_option(self):
        cluster = self.run_and_get_cluster(
            '--iam-instance-profile', 'EMR_EC2_DefaultRole')
        self.assertTrue(boto.connect_iam.called)

        self.assertEqual(job_flow.jobflowrole, 'EMR_EC2_DefaultRole')

    def test_deprecated_job_flow_role_option(self):
        with logger_disabled('mrjob.emr'):
            cluster = self.run_and_get_cluster(
                '--iam-job-flow-role', 'EMR_EC2_DefaultRole')
            self.assertTrue(boto.connect_iam.called)

            self.assertEqual(job_flow.jobflowrole, 'EMR_EC2_DefaultRole')

    def test_iam_service_role_option(self):
        cluster = self.run_and_get_cluster(
            '--iam-service-role', 'EMR_DefaultRole')
        self.assertTrue(boto.connect_iam.called)

        self.assertEqual(job_flow.servicerole, 'EMR_DefaultRole')

    def test_both_iam_options(self):
        cluster = self.run_and_get_cluster(
            '--iam-instance-profile', 'EMR_EC2_DefaultRole',
            '--iam-service-role', 'EMR_DefaultRole')

        # users with limited access may not be able to connect to the IAM API.
        # This gives them a plan B
        self.assertFalse(boto.connect_iam.called)

        self.assertEqual(job_flow.jobflowrole, 'EMR_EC2_DefaultRole')
        self.assertEqual(job_flow.servicerole, 'EMR_DefaultRole')

    def test_no_iam_access(self):
        ex = boto.exception.BotoServerError(403, 'Forbidden')
        self.assertIsInstance(boto.connect_iam, Mock)
        boto.connect_iam.side_effect = ex

        with logger_disabled('mrjob.emr'):
            cluster = self.run_and_get_cluster()

        self.assertTrue(boto.connect_iam.called)

        self.assertEqual(job_flow.jobflowrole, 'EMR_EC2_DefaultRole')
        self.assertEqual(job_flow.servicerole, 'EMR_DefaultRole')


class EMRAPIParamsTestCase(MockEMRAndS3TestCase):

    def test_param_set(self):
        cluster = self.run_and_get_cluster('--emr-api-param', 'Test.API=a', '--emr-api-param', 'Test.API2=b')
        self.assertTrue('Test.API' in job_flow.api_params)
        self.assertTrue('Test.API2' in job_flow.api_params)
        self.assertEqual(job_flow.api_params['Test.API'], 'a')
        self.assertEqual(job_flow.api_params['Test.API2'], 'b')

    def test_param_unset(self):
        cluster = self.run_and_get_cluster('--no-emr-api-param', 'Test.API', '--no-emr-api-param', 'Test.API2')
        self.assertTrue('Test.API' in job_flow.api_params)
        self.assertTrue('Test.API2' in job_flow.api_params)
        self.assertIsNone(job_flow.api_params['Test.API'])
        self.assertIsNone(job_flow.api_params['Test.API2'])

    def test_invalid_param(self):
        self.assertRaises(ValueError, self.run_and_get_cluster, '--emr-api-param', 'Test.API')

    def test_overrides(self):
        cluster = self.run_and_get_cluster('--emr-api-param', 'VisibleToAllUsers=false', '--visible-to-all-users')
        self.assertEqual(job_flow.visibletoallusers, 'false')

    def test_no_emr_api_param_command_line_switch(self):
        job = MRWordCount([
            '-r', 'emr',
            '--emr-api-param', 'Instance.Ec2SubnetId=someID',
            '--no-emr-api-param', 'VisibleToAllUsers'])

        with job.make_runner() as runner:
            self.assertEqual(runner._opts['emr_api_params'],
                             {'Instance.Ec2SubnetId': 'someID',
                              'VisibleToAllUsers': None})

    def test_no_emr_api_params_is_not_a_real_option(self):
        job = MRWordCount([
            '-r', 'emr',
            '--no-emr-api-param', 'VisibleToAllUsers'])

        self.assertNotIn('no_emr_api_params',
                         sorted(job.emr_job_runner_kwargs()))
        self.assertNotIn('no_emr_api_param',
                         sorted(job.emr_job_runner_kwargs()))

        with job.make_runner() as runner:
            self.assertNotIn('no_emr_api_params', sorted(runner._opts))
            self.assertNotIn('no_emr_api_param', sorted(runner._opts))
            self.assertEqual(runner._opts['emr_api_params'],
                            {'VisibleToAllUsers': None})

    def test_command_line_overrides_config(self):
        # want to make sure a nulled-out param in the config file
        # can't override a param set on the command line

        API_PARAMS_MRJOB_CONF = {'runners': {'emr': {
            'check_emr_status_every': 0.00,
            's3_sync_wait_time': 0.00,
            'emr_api_params': {
                'Instance.Ec2SubnetId': 'someID',
                'VisibleToAllUsers': None,
                'Name': 'eaten_by_a_whale',
            },
        }}}

        job = MRWordCount([
            '-r', 'emr',
            '--no-emr-api-param', 'Instance.Ec2SubnetId',
            '--emr-api-param', 'VisibleToAllUsers=true'])

        with mrjob_conf_patcher(API_PARAMS_MRJOB_CONF):
            with job.make_runner() as runner:
                self.assertEqual(runner._opts['emr_api_params'],
                    {'Instance.Ec2SubnetId': None,
                     'VisibleToAllUsers': 'true',
                     'Name': 'eaten_by_a_whale'})


class AMIAndHadoopVersionTestCase(MockEMRAndS3TestCase):

    def test_default(self):
        with self.make_runner() as runner:
            runner.run()
            # default is "latest"
            self.assertEqual(runner.get_ami_version(), '2.4.2')
            self.assertEqual(runner.get_hadoop_version(), '1.0.3')

    def test_ami_version_1_0_no_longer_supported(self):
        with self.make_runner('--ami-version', '1.0') as runner:
            self.assertRaises(boto.exception.EmrResponseError,
                              runner._launch)

    def test_ami_version_2_0(self):
        with self.make_runner('--ami-version', '2.0') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '2.0.6')
            self.assertEqual(runner.get_hadoop_version(), '0.20.205')

    def test_latest_ami_version(self):
        # "latest" is no longer actually the latest version
        with self.make_runner('--ami-version', 'latest') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '2.4.2')
            self.assertEqual(runner.get_hadoop_version(), '1.0.3')

    def test_ami_version_3_0(self):
        with self.make_runner('--ami-version', '3.0',
                              '--ec2-instance-type', 'm1.medium') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '3.0.4')
            self.assertEqual(runner.get_hadoop_version(), '2.2.0')

    def test_ami_version_3_8_0(self):
        with self.make_runner('--ami-version', '3.8.0',
                              '--ec2-instance-type', 'm1.medium') as runner:
            runner.run()
            self.assertEqual(runner.get_ami_version(), '3.8.0')
            self.assertEqual(runner.get_hadoop_version(), '2.4.0')

    def test_hadoop_version_option_does_nothing(self):
        with logger_disabled('mrjob.emr'):
            with self.make_runner('--hadoop-version', '1.2.3.4') as runner:
                runner.run()
                self.assertEqual(runner.get_ami_version(), '2.4.2')
                self.assertEqual(runner.get_hadoop_version(), '1.0.3')


class AvailabilityZoneTestCase(MockEMRAndS3TestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
        'aws_availability_zone': 'PUPPYLAND',
    }}}

    def test_availability_zone_config(self):
        with self.make_runner() as runner:
            runner.run()

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.ec2instanceattributes.ec2availabilityzone,
                             'PUPPYLAND')


class EnableDebuggingTestCase(MockEMRAndS3TestCase):

    def test_debugging_works(self):
        with self.make_runner('--enable-emr-debugging') as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            steps = list(_yield_all_steps(emr_conn, runner.get_cluster_id()))

            self.assertEqual(steps[0].name, 'Setup Hadoop Debugging')


class BucketRegionTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(BucketRegionTestCase, self).setUp()
        self.make_dummy_data()

    def make_dummy_data(self):
        self.add_mock_s3_data({'mrjob-1': {}})
        s3c = boto.connect_s3()
        self.bucket1 = s3c.get_bucket('mrjob-1')
        self.bucket1_uri = 's3://mrjob-1/tmp/'

    def test_region_nobucket_nolocation(self):
        # aws_region specified, no bucket specified, default bucket has no
        # location
        j = EMRJobRunner(aws_region='PUPPYLAND',
                         s3_endpoint='PUPPYLAND',
                         conf_paths=[])
        self.assertNotEqual(j._opts['s3_scratch_uri'], self.bucket1_uri)

    def test_region_nobucket_nomatchexists(self):
        # aws_region specified, no bucket specified, no buckets have matching
        # region
        self.bucket1.set_location('PUPPYLAND')
        j = EMRJobRunner(aws_region='KITTYLAND',
                         s3_endpoint='KITTYLAND',
                         conf_paths=[])
        self.assertNotEqual(j._opts['s3_scratch_uri'], self.bucket1_uri)

    def test_noregion_nobucket_nolocation(self):
        # aws_region not specified, no bucket specified, default bucket has no
        # location
        j = EMRJobRunner(conf_paths=[])
        self.assertEqual(j._opts['s3_scratch_uri'], self.bucket1_uri)

    def test_noregion_bucket_nolocation(self):
        # aws_region not specified, bucket specified without location
        j = EMRJobRunner(conf_paths=[],
                         s3_scratch_uri=self.bucket1_uri)
        self.assertEqual(j._opts['s3_scratch_uri'], self.bucket1_uri)

    def test_noregion_bucket_location(self):
        # aws_region not specified, bucket specified with location
        self.bucket1.set_location('PUPPYLAND')
        j = EMRJobRunner(conf_paths=[])
        self.assertEqual(j._aws_region, 'PUPPYLAND')


class ExtraBucketRegionTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(ExtraBucketRegionTestCase, self).setUp()
        self.make_dummy_data()

    def make_dummy_data(self):
        self.add_mock_s3_data({'mrjob-1': {}})
        s3c = boto.connect_s3()
        self.bucket1 = s3c.get_bucket('mrjob-1')
        self.bucket1_uri = 's3://mrjob-1/tmp/'

        self.add_mock_s3_data({'mrjob-2': {}})
        self.bucket2 = s3c.get_bucket('mrjob-2')
        self.bucket2.set_location('KITTYLAND')
        self.bucket2_uri = 's3://mrjob-2/tmp/'

    def test_region_nobucket_matchexists(self):
        # aws_region specified, no bucket specified, bucket exists with
        # matching region
        j = EMRJobRunner(aws_region='KITTYLAND',
                         s3_endpoint='KITTYLAND',
                         conf_paths=[])
        self.assertEqual(j._opts['s3_scratch_uri'], self.bucket2_uri)

    def test_region_bucket_match(self):
        # aws_region specified, bucket specified with matching location
        j = EMRJobRunner(aws_region='PUPPYLAND',
                         s3_endpoint='PUPPYLAND',
                         s3_scratch_uri=self.bucket1_uri,
                         conf_paths=[])
        self.assertEqual(j._opts['s3_scratch_uri'], self.bucket1_uri)

    def test_region_bucket_does_not_match(self):
        # aws_region specified, bucket specified with incorrect location
        with no_handlers_for_logger():
            stderr = StringIO()
            log = logging.getLogger('mrjob.emr')
            log.addHandler(logging.StreamHandler(stderr))
            log.setLevel(logging.WARNING)

            EMRJobRunner(aws_region='PUPPYLAND',
                         s3_endpoint='PUPPYLAND',
                         s3_scratch_uri=self.bucket2_uri,
                         conf_paths=[])

            self.assertIn('does not match bucket region', stderr.getvalue())


class EC2InstanceGroupTestCase(MockEMRAndS3TestCase):

    def _test_instance_groups(self, opts, **expected):
        """Run a job with the given option dictionary, and check for
        for instance, number, and optional bid price for each instance role.

        Specify expected instance group info like:

        <role>=(num_instances, instance_type, bid_price)
        """
        runner = EMRJobRunner(**opts)

        job_flow_id = runner.make_persistent_job_flow()
        job_flow = runner.make_emr_conn().describe_jobflow(job_flow_id)

        # convert expected to a dict of dicts
        role_to_expected = {}
        for role, (num, instance_type, bid_price) in expected.iteritems():
            info = {
                'instancerequestcount': str(num),
                'instancetype': instance_type,
            }
            if bid_price:
                info['market'] = 'SPOT'
                info['bidprice'] = bid_price
            else:
                info['market'] = 'ON_DEMAND'

            role_to_expected[role.upper()] = info

        # convert actual instance groups to dicts
        role_to_actual = {}
        for ig in job_flow.instancegroups:
            info = {}
            for field in ('bidprice', 'instancerequestcount',
                          'instancetype', 'market'):
                if hasattr(ig, field):
                    info[field] = getattr(ig, field)
            role_to_actual[ig.instancerole] = info

        self.assertEqual(role_to_expected, role_to_actual)

        # also check master/slave and # of instance types
        # this is mostly a sanity check of mockboto
        expected_master_instance_type = role_to_expected.get(
            'MASTER', {}).get('instancetype')
        self.assertEqual(expected_master_instance_type,
                         getattr(job_flow, 'masterinstancetype', None))

        expected_slave_instance_type = role_to_expected.get(
            'CORE', {}).get('instancetype')
        self.assertEqual(expected_slave_instance_type,
                         getattr(job_flow, 'slaveinstancetype', None))

        expected_instance_count = str(sum(
            int(info['instancerequestcount'])
            for info in role_to_expected.itervalues()))
        self.assertEqual(expected_instance_count, job_flow.instancecount)

    def set_in_mrjob_conf(self, **kwargs):
        emr_opts = copy.deepcopy(self.MRJOB_CONF_CONTENTS)
        emr_opts['runners']['emr'].update(kwargs)
        patcher = mrjob_conf_patcher(emr_opts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults(self):
        self._test_instance_groups(
            {},
            master=(1, 'm1.small', None))

        self._test_instance_groups(
            {'num_ec2_instances': 3},
            core=(2, 'm1.small', None),
            master=(1, 'm1.small', None))

    def test_single_instance(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            master=(1, 'c1.xlarge', None))

    def test_multiple_instances(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge', 'num_ec2_instances': 3},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.small', None))

    def test_explicit_master_and_slave_instance_types(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.small', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

    def test_explicit_instance_types_take_precedence(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge',
             'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge',
             'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

    def test_cmd_line_opts_beat_mrjob_conf(self):
        # set ec2_instance_type in mrjob.conf, 1 instance
        self.set_in_mrjob_conf(ec2_instance_type='c1.xlarge')

        self._test_instance_groups(
            {},
            master=(1, 'c1.xlarge', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        # set ec2_instance_type in mrjob.conf, 3 instances
        self.set_in_mrjob_conf(ec2_instance_type='c1.xlarge',
                               num_ec2_instances=3)

        self._test_instance_groups(
            {},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.small', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge'},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

        # set master in mrjob.conf, 1 instance
        self.set_in_mrjob_conf(ec2_master_instance_type='m1.large')

        self._test_instance_groups(
            {},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            master=(1, 'c1.xlarge', None))

        # set master and slave in mrjob.conf, 2 instances
        self.set_in_mrjob_conf(ec2_master_instance_type='m1.large',
                               ec2_slave_instance_type='m2.xlarge',
                               num_ec2_instances=3)

        self._test_instance_groups(
            {},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.large', None))

    def test_zero_core_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'c1.medium',
             'num_ec2_core_instances': 0},
            master=(1, 'c1.medium', None))

    def test_core_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 5},
            core=(5, 'c1.medium', '0.20'),
            master=(1, 'm1.large', None))

    def test_core_on_demand_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5},
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None))

        # Test the ec2_slave_instance_type alias
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'c1.medium',
             'num_ec2_instances': 6},
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None))

    def test_core_and_task_on_demand_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5,
             'ec2_task_instance_type': 'm2.xlarge',
             'num_ec2_task_instances': 20,
             },
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', None))

    def test_core_and_task_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 10,
             'ec2_task_instance_type': 'm2.xlarge',
             'ec2_task_instance_bid_price': '1.00',
             'num_ec2_task_instances': 20,
             },
            core=(10, 'c1.medium', '0.20'),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', '1.00'))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 10,
             'ec2_task_instance_type': 'm2.xlarge',
             'ec2_task_instance_bid_price': '1.00',
             'num_ec2_task_instances': 20,
             },
            core=(10, 'c1.medium', None),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', '1.00'))

    def test_master_and_core_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_master_instance_bid_price': '0.50',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 10,
             },
            core=(10, 'c1.medium', '0.20'),
            master=(1, 'm1.large', '0.50'))

    def test_master_spot_instance(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_master_instance_bid_price': '0.50',
             },
            master=(1, 'm1.large', '0.50'))

    def test_zero_or_blank_bid_price_means_on_demand(self):
        self._test_instance_groups(
            {'ec2_master_instance_bid_price': '0',
             },
            master=(1, 'm1.small', None))

        self._test_instance_groups(
            {'num_ec2_core_instances': 3,
             'ec2_core_instance_bid_price': '0.00',
             },
            core=(3, 'm1.small', None),
            master=(1, 'm1.small', None))

        self._test_instance_groups(
            {'num_ec2_core_instances': 3,
             'num_ec2_task_instances': 5,
             'ec2_task_instance_bid_price': '',
             },
            core=(3, 'm1.small', None),
            master=(1, 'm1.small', None),
            task=(5, 'm1.small', None))

    def test_pass_invalid_bid_prices_through_to_emr(self):
        self.assertRaises(
            boto.exception.EmrResponseError,
            self._test_instance_groups,
            {'ec2_master_instance_bid_price': 'all the gold in California'})

    def test_task_type_defaults_to_core_type(self):
        self._test_instance_groups(
            {'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5,
             'num_ec2_task_instances': 20,
             },
            core=(5, 'c1.medium', None),
            master=(1, 'm1.small', None),
            task=(20, 'c1.medium', None))

    def test_mixing_instance_number_opts_on_cmd_line(self):
        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.emr', stderr)
            self._test_instance_groups(
                {'num_ec2_instances': 4,
                 'num_ec2_core_instances': 10},
                core=(10, 'm1.small', None),
                master=(1, 'm1.small', None))

        self.assertIn('does not make sense', stderr.getvalue())

    def test_mixing_instance_number_opts_in_mrjob_conf(self):
        self.set_in_mrjob_conf(num_ec2_instances=3,
                               num_ec2_core_instances=5,
                               num_ec2_task_instances=9)

        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.emr', stderr)
            self._test_instance_groups(
                {},
                core=(5, 'm1.small', None),
                master=(1, 'm1.small', None),
                task=(9, 'm1.small', None))

        self.assertIn('does not make sense', stderr.getvalue())

    def test_cmd_line_instance_numbers_beat_mrjob_conf(self):
        self.set_in_mrjob_conf(num_ec2_core_instances=5,
                               num_ec2_task_instances=9)

        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.emr', stderr)
            self._test_instance_groups(
                {'num_ec2_instances': 3},
                core=(2, 'm1.small', None),
                master=(1, 'm1.small', None))

        self.assertNotIn('does not make sense', stderr.getvalue())


### tests for error parsing ###

BUCKET = 'walrus'
BUCKET_URI = 's3://' + BUCKET + '/'

LOG_DIR = 'j-JOBFLOWID/'

GARBAGE = \
"""GarbageGarbageGarbage
"""

TRACEBACK_START = 'Traceback (most recent call last):\n'

PY_EXCEPTION = \
"""  File "<string>", line 1, in <module>
TypeError: 'int' object is not iterable
"""

CHILD_ERR_LINE = (
    '2010-07-27 18:25:48,397 WARN'
    ' org.apache.hadoop.mapred.TaskTracker (main): Error running child\n')

JAVA_STACK_TRACE = """java.lang.OutOfMemoryError: Java heap space
        at org.apache.hadoop.mapred.IFile$Reader.readNextBlock(IFile.java:270)
        at org.apache.hadoop.mapred.IFile$Reader.next(IFile.java:332)
"""

HADOOP_ERR_LINE_PREFIX = ('2010-07-27 19:53:35,451 ERROR'
                          ' org.apache.hadoop.streaming.StreamJob (main): ')

USEFUL_HADOOP_ERROR = (
    'Error launching job , Output path already exists :'
    ' Output directory s3://yourbucket/logs/2010/07/23/ already exists'
    ' and is not empty')

BORING_HADOOP_ERROR = 'Job not Successful!'
TASK_ATTEMPTS_DIR = LOG_DIR + 'task-attempts/'

ATTEMPT_0_DIR = TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_0/'
ATTEMPT_1_DIR = TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_0/'


def make_input_uri_line(input_uri):
    return ("2010-07-27 17:55:29,400 INFO"
            " org.apache.hadoop.fs.s3native.NativeS3FileSystem (main):"
            " Opening '%s' for reading\n" % input_uri)


class FindProbableCauseOfFailureTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(FindProbableCauseOfFailureTestCase, self).setUp()
        self.make_runner()

    def tearDown(self):
        self.cleanup_runner()
        super(FindProbableCauseOfFailureTestCase, self).tearDown()

    # We're mostly concerned here that the right log files are read in the
    # right order. parsing of the logs is handled by tests.parse_test
    def make_runner(self):
        self.add_mock_s3_data({'walrus': {}})
        self.runner = EMRJobRunner(s3_sync_wait_time=0,
                                   s3_scratch_uri='s3://walrus/tmp',
                                   conf_paths=[])
        self.runner._s3_job_log_uri = BUCKET_URI + LOG_DIR

    def cleanup_runner(self):
        self.runner.cleanup()

    def test_empty(self):
        self.add_mock_s3_data({'walrus': {}})
        self.assertEqual(self.runner._find_probable_cause_of_failure([1]),
                         None)

    def test_python_exception(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr':
                GARBAGE + TRACEBACK_START + PY_EXCEPTION + GARBAGE,
            ATTEMPT_0_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz'),
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': list(StringIO(TRACEBACK_START + PY_EXCEPTION)),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'stderr',
             'input_uri': BUCKET_URI + 'input.gz'})

    def test_python_exception_without_input_uri(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr': (
                GARBAGE + TRACEBACK_START + PY_EXCEPTION + GARBAGE),
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': list(StringIO(TRACEBACK_START + PY_EXCEPTION)),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'stderr',
             'input_uri': None})

    def test_java_exception(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr': GARBAGE + GARBAGE,
            ATTEMPT_0_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz') +
                GARBAGE +
                CHILD_ERR_LINE +
                JAVA_STACK_TRACE +
                GARBAGE,
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': list(StringIO(JAVA_STACK_TRACE)),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'syslog',
             'input_uri': BUCKET_URI + 'input.gz'})

    def test_java_exception_without_input_uri(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'syslog':
                CHILD_ERR_LINE +
                JAVA_STACK_TRACE +
                GARBAGE,
        }})
        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1]),
            {'lines': list(StringIO(JAVA_STACK_TRACE)),
             'log_file_uri': BUCKET_URI + ATTEMPT_0_DIR + 'syslog',
             'input_uri': None})

    def test_hadoop_streaming_error(self):
        # we should look only at step 2 since the errors in the other
        # steps are boring
        #
        # we include input.gz just to test that we DON'T check for it
        self.add_mock_s3_data({'walrus': {
            LOG_DIR + 'steps/1/syslog':
                GARBAGE +
                HADOOP_ERR_LINE_PREFIX + BORING_HADOOP_ERROR + '\n',
            LOG_DIR + 'steps/2/syslog':
                GARBAGE +
                make_input_uri_line(BUCKET_URI + 'input.gz') +
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + '\n',
            LOG_DIR + 'steps/3/syslog':
                HADOOP_ERR_LINE_PREFIX + BORING_HADOOP_ERROR + '\n',
        }})

        self.assertEqual(
            self.runner._find_probable_cause_of_failure([1, 2, 3]),
            {'lines': [USEFUL_HADOOP_ERROR + '\n'],
             'log_file_uri': BUCKET_URI + LOG_DIR + 'steps/2/syslog',
             'input_uri': None})

    def test_later_task_attempt_steps_win(self):
        # should look at later steps first
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_r_000126_3/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000004_0/syslog':
                CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0002_m_000004_0/syslog')

    def test_later_step_logs_win(self):
        self.add_mock_s3_data({'walrus': {
            LOG_DIR + 'steps/1/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + '\n',
            LOG_DIR + 'steps/2/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + '\n',
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + LOG_DIR + 'steps/2/syslog')

    def test_reducer_beats_mapper(self):
        # should look at reducers over mappers
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_3/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_r_000126_3/syslog':
                CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0001_r_000126_3/syslog')

    def test_more_attempts_win(self):
        # look at fourth attempt before looking at first attempt
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0001_m_000004_3/syslog':
                CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0001_m_000004_3/syslog')

    def test_py_exception_beats_java_stack_trace(self):
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr': TRACEBACK_START + PY_EXCEPTION,
            ATTEMPT_0_DIR + 'syslog': CHILD_ERR_LINE + JAVA_STACK_TRACE,
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + ATTEMPT_0_DIR + 'stderr')

    def test_exception_beats_hadoop_error(self):
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            LOG_DIR + 'steps/1/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + '\n',
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0002_m_000126_0/stderr')

    def test_step_filtering(self):
        # same as previous test, but step 2 is filtered out
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
            LOG_DIR + 'steps/1/syslog':
                HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + '\n',
        }})
        failure = self.runner._find_probable_cause_of_failure([1])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + LOG_DIR + 'steps/1/syslog')

    def test_ignore_errors_from_steps_that_later_succeeded(self):
        # This tests the fix for Issue #31
        self.add_mock_s3_data({'walrus': {
            ATTEMPT_0_DIR + 'stderr':
                GARBAGE + TRACEBACK_START + PY_EXCEPTION + GARBAGE,
            ATTEMPT_0_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz'),
            ATTEMPT_1_DIR + 'stderr': '',
            ATTEMPT_1_DIR + 'syslog':
                make_input_uri_line(BUCKET_URI + 'input.gz'),
        }})
        self.assertEqual(self.runner._find_probable_cause_of_failure([1]),
                         None)


class CounterFetchingTestCase(MockEMRAndS3TestCase):

    COUNTER_LINE = (
        'Job JOBID="job_201106092314_0001" FINISH_TIME="1307662284564"'
        ' JOB_STATUS="SUCCESS" FINISHED_MAPS="0" FINISHED_REDUCES="0"'
        ' FAILED_MAPS="0" FAILED_REDUCES="0" COUNTERS="%s" .' % ''.join([
            '{(org\.apache\.hadoop\.mapred\.JobInProgress$Counter)',
            '(Job Counters )',
            '[(TOTAL_LAUNCHED_REDUCES)(Launched reduce tasks)(1)]}',
    ]))

    def setUp(self):
        super(CounterFetchingTestCase, self).setUp()
        self.add_mock_s3_data({'walrus': {}})
        kwargs = {
            'conf_paths': [],
            's3_scratch_uri': 's3://walrus/',
            's3_sync_wait_time': 0}
        with EMRJobRunner(**kwargs) as runner:
            self.job_flow_id = runner.make_persistent_job_flow()
        self.runner = EMRJobRunner(emr_job_flow_id=self.job_flow_id, **kwargs)

    def tearDown(self):
        super(CounterFetchingTestCase, self).tearDown()
        self.runner.cleanup()

    def test_empty_counters_running_job(self):
        self.runner._describe_jobflow().state = 'RUNNING'
        with no_handlers_for_logger():
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)
            self.runner._fetch_counters([1], skip_s3_wait=True)
            self.assertIn('5 minutes', stderr.getvalue())

    def test_present_counters_running_job(self):
        self.add_mock_s3_data({'walrus': {
            'logs/j-MOCKCLUSTER0/jobs/job_0_1_hadoop_streamjob1.jar':
            self.COUNTER_LINE}})
        self.runner._describe_jobflow().state = 'RUNNING'
        self.runner._fetch_counters([1], skip_s3_wait=True)
        self.assertEqual(self.runner.counters(),
                         [{'Job Counters ': {'Launched reduce tasks': 1}}])

    def test_present_counters_terminated_job(self):
        self.add_mock_s3_data({'walrus': {
            'logs/j-MOCKCLUSTER0/jobs/job_0_1_hadoop_streamjob1.jar':
            self.COUNTER_LINE}})
        self.runner._describe_jobflow().state = 'TERMINATED'
        self.runner._fetch_counters([1], skip_s3_wait=True)
        self.assertEqual(self.runner.counters(),
                         [{'Job Counters ': {'Launched reduce tasks': 1}}])

    def test_present_counters_step_mismatch(self):
        self.add_mock_s3_data({'walrus': {
            'logs/j-MOCKCLUSTER0/jobs/job_0_1_hadoop_streamjob1.jar':
            self.COUNTER_LINE}})
        self.runner._describe_jobflow().state = 'RUNNING'
        self.runner._fetch_counters([2], {2: 1}, skip_s3_wait=True)
        self.assertEqual(self.runner.counters(),
                         [{'Job Counters ': {'Launched reduce tasks': 1}}])

    def test_zero_log_generating_steps(self):
        mock_steps = [
            MockEmrObject(jar='x.jar',
                          name=self.runner._job_name,
                          state='COMPLETED'),
            MockEmrObject(jar='x.jar',
                          name=self.runner._job_name,
                          state='COMPLETED'),
        ]
        mock_jobflow = MockEmrObject(state='COMPLETED',
                                    steps=mock_steps)
        self.runner._describe_jobflow = Mock(return_value=mock_jobflow)
        self.runner._fetch_counters_s3 = Mock(return_value={})
        self.runner._wait_for_job_to_complete()
        self.runner._fetch_counters_s3.assert_called_with([], False)

    def test_interleaved_log_generating_steps(self):
        mock_steps = [
            MockEmrObject(jar='x.jar',
                          name=self.runner._job_name,
                          state='COMPLETED'),
            MockEmrObject(jar='hadoop.streaming.jar',
                          name=self.runner._job_name,
                          state='COMPLETED'),
            MockEmrObject(jar='x.jar',
                          name=self.runner._job_name,
                          state='COMPLETED'),
            MockEmrObject(jar='hadoop.streaming.jar',
                          name=self.runner._job_name,
                          state='COMPLETED'),
        ]
        mock_jobflow = MockEmrObject(state='COMPLETED',
                                    steps=mock_steps)
        self.runner._describe_jobflow = Mock(return_value=mock_jobflow)
        self.runner._fetch_counters_s3 = Mock(return_value={})
        self.runner._wait_for_job_to_complete()
        self.runner._fetch_counters_s3.assert_called_with([1, 2], False)


class LogFetchingFallbackTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(LogFetchingFallbackTestCase, self).setUp()
        # Make sure that SSH and S3 are accessed when we expect them to be
        self.add_mock_s3_data({'walrus': {}})

        self.runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp')
        self.runner._s3_job_log_uri = BUCKET_URI + LOG_DIR
        self.prepare_runner_for_ssh(self.runner)

    def tearDown(self):
        super(LogFetchingFallbackTestCase, self).tearDown()
        """This method assumes ``prepare_runner_for_ssh()`` was called. That
        method isn't a "proper" setup method because it requires different
        arguments for different tests.
        """
        self.runner.cleanup()

    def test_ssh_comes_first(self):
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/steps/1')
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/history')
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/userlogs')

        # Put a log file and error into SSH
        ssh_lone_log_path = posixpath.join(
            SSH_LOG_ROOT, 'steps', '1', 'syslog')
        mock_ssh_file('testmaster', ssh_lone_log_path,
                      HADOOP_ERR_LINE_PREFIX + USEFUL_HADOOP_ERROR + '\n')

        # Put a 'more interesting' error in S3 to make sure that the
        # 'less interesting' one from SSH is read and S3 is never
        # looked at. This would never happen in reality because the
        # logs should be identical, but it makes for an easy test
        # of SSH overriding S3.
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         SSH_PREFIX + self.runner._address + ssh_lone_log_path)

    def test_ssh_works_with_slaves(self):
        self.add_slave()

        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/steps/1')
        mock_ssh_dir('testmaster', SSH_LOG_ROOT + '/history')
        mock_ssh_dir(
            'testmaster!testslave0',
            SSH_LOG_ROOT + '/userlogs/attempt_201007271720_0002_m_000126_0')

        # Put a log file and error into SSH
        ssh_log_path = posixpath.join(SSH_LOG_ROOT, 'userlogs',
                                      'attempt_201007271720_0002_m_000126_0',
                                      'stderr')
        ssh_log_path_2 = posixpath.join(SSH_LOG_ROOT, 'userlogs',
                                        'attempt_201007271720_0002_m_000126_0',
                                        'syslog')
        mock_ssh_file('testmaster!testslave0', ssh_log_path,
                      TRACEBACK_START + PY_EXCEPTION)
        mock_ssh_file('testmaster!testslave0', ssh_log_path_2,
                      '')
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         SSH_PREFIX + 'testmaster!testslave0' + ssh_log_path)

    def test_ssh_fails_to_s3(self):
        # the runner will try to use SSH and find itself unable to do so,
        # throwing a LogFetchError and triggering S3 fetching.
        self.runner._address = None

        # Put a different error into S3
        self.add_mock_s3_data({'walrus': {
            TASK_ATTEMPTS_DIR + 'attempt_201007271720_0002_m_000126_0/stderr':
                TRACEBACK_START + PY_EXCEPTION,
        }})
        failure = self.runner._find_probable_cause_of_failure([1, 2])
        self.assertEqual(failure['log_file_uri'],
                         BUCKET_URI + TASK_ATTEMPTS_DIR +
                         'attempt_201007271720_0002_m_000126_0/stderr')


class TestEMRandS3Endpoints(MockEMRAndS3TestCase):

    def test_no_region(self):
        runner = EMRJobRunner(conf_paths=[])
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3.amazonaws.com')
        self.assertEqual(runner._aws_region, '')

    def test_none_region(self):
        # blank region should be treated the same as no region
        runner = EMRJobRunner(conf_paths=[], aws_region=None)
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3.amazonaws.com')
        self.assertEqual(runner._aws_region, '')

    def test_blank_region(self):
        # blank region should be treated the same as no region
        runner = EMRJobRunner(conf_paths=[], aws_region='')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3.amazonaws.com')
        self.assertEqual(runner._aws_region, '')

    def test_eu(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='EU')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.eu-west-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3-eu-west-1.amazonaws.com')

    def test_eu_case_insensitive(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='eu')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.eu-west-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3-eu-west-1.amazonaws.com')

    def test_us_east_1(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='us-east-1')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.us-east-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3.amazonaws.com')

    def test_us_west_1(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='us-west-1')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.us-west-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3-us-west-1.amazonaws.com')

    def test_us_west_1_case_insensitive(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='US-West-1')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.us-west-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3-us-west-1.amazonaws.com')

    def test_ap_southeast_1(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='ap-southeast-1')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.ap-southeast-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3-ap-southeast-1.amazonaws.com')

    def test_previously_unknown_region(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='lolcatnia-1')
        self.assertEqual(runner.make_emr_conn().endpoint,
                         'elasticmapreduce.lolcatnia-1.amazonaws.com')
        self.assertEqual(runner.make_s3_conn().endpoint,
                         's3-lolcatnia-1.amazonaws.com')

    def test_explicit_endpoints(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='EU',
                              s3_endpoint='s3-proxy', emr_endpoint='emr-proxy')
        self.assertEqual(runner.make_emr_conn().endpoint, 'emr-proxy')
        self.assertEqual(runner.make_s3_conn().endpoint, 's3-proxy')

    def test_ssl_fallback_host(self):
        runner = EMRJobRunner(conf_paths=[], aws_region='us-west-1')

        with patch.object(MockEmrConnection, 'STRICT_SSL', True):
            emr_conn = runner.make_emr_conn()
            self.assertEqual(emr_conn.endpoint,
                             'elasticmapreduce.us-west-1.amazonaws.com')
            # this should still work
            self.assertEqual(emr_conn.describe_jobflows(), [])
            # but it's only because we've switched to the alternate hostname
            self.assertEqual(emr_conn.endpoint,
                             'us-west-1.elasticmapreduce.amazonaws.com')

        # without SSL issues, we should stay on the same endpoint
        emr_conn = runner.make_emr_conn()
        self.assertEqual(emr_conn.endpoint,
                         'elasticmapreduce.us-west-1.amazonaws.com')
        self.assertEqual(emr_conn.describe_jobflows(), [])
        self.assertEqual(emr_conn.endpoint,
                         'elasticmapreduce.us-west-1.amazonaws.com')


class TestS3Ls(MockEMRAndS3TestCase):

    def test_s3_ls(self):
        self.add_mock_s3_data({'walrus': {'one': '', 'two': '', 'three': ''}})

        runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp',
                              conf_paths=[])

        self.assertEqual(set(runner._s3_ls('s3://walrus/')),
                         set(['s3://walrus/one',
                              's3://walrus/two',
                              's3://walrus/three',
                              ]))

        self.assertEqual(set(runner._s3_ls('s3://walrus/t')),
                         set(['s3://walrus/two',
                              's3://walrus/three',
                              ]))

        self.assertEqual(set(runner._s3_ls('s3://walrus/t/')),
                         set([]))

        # if we ask for a nonexistent bucket, we should get some sort
        # of exception (in practice, buckets with random names will
        # probably be owned by other people, and we'll get some sort
        # of permissions error)
        self.assertRaises(Exception, set, runner._s3_ls('s3://lolcat/'))


class TestSSHLs(MockEMRAndS3TestCase):

    def setUp(self):
        super(TestSSHLs, self).setUp()
        self.make_runner()

    def tearDown(self):
        super(TestSSHLs, self).tearDown()

    def make_runner(self):
        self.runner = EMRJobRunner(conf_paths=[])
        self.prepare_runner_for_ssh(self.runner)

    def test_ssh_ls(self):
        self.add_slave()

        mock_ssh_dir('testmaster', 'test')
        mock_ssh_file('testmaster', posixpath.join('test', 'one'), '')
        mock_ssh_file('testmaster', posixpath.join('test', 'two'), '')
        mock_ssh_dir('testmaster!testslave0', 'test')
        mock_ssh_file('testmaster!testslave0',
                      posixpath.join('test', 'three'), '')

        self.assertEqual(
            sorted(self.runner.ls('ssh://testmaster/test')),
            ['ssh://testmaster/test/one', 'ssh://testmaster/test/two'])

        self.runner._enable_slave_ssh_access()

        self.assertEqual(
            list(self.runner.ls('ssh://testmaster!testslave0/test')),
            ['ssh://testmaster!testslave0/test/three'])

        # ls() is a generator, so the exception won't fire until we list() it
        self.assertRaises(IOError, list,
                          self.runner.ls('ssh://testmaster/does_not_exist'))


class TestNoBoto(unittest.TestCase):

    def setUp(self):
        self.blank_out_boto()

    def tearDown(self):
        self.restore_boto()

    def blank_out_boto(self):
        self._real_boto = mrjob.emr.boto
        mrjob.emr.boto = None
        mrjob.fs.s3.boto = None

    def restore_boto(self):
        mrjob.emr.boto = self._real_boto
        mrjob.fs.s3.boto = self._real_boto

    def test_init(self):
        # merely creating an EMRJobRunner should raise an exception
        # because it'll need to connect to S3 to set s3_scratch_uri
        self.assertRaises(ImportError, EMRJobRunner, conf_paths=[])

    def test_init_with_s3_scratch_uri(self):
        # this also raises an exception because we have to check
        # the bucket location
        self.assertRaises(ImportError, EMRJobRunner,
                          conf_paths=[], s3_scratch_uri='s3://foo/tmp')


class TestMasterBootstrapScript(MockEMRAndS3TestCase):

    def setUp(self):
        super(TestMasterBootstrapScript, self).setUp()
        self.make_tmp_dir()

    def tearDown(self):
        super(TestMasterBootstrapScript, self).tearDown()
        self.rm_tmp_dir()

    def make_tmp_dir(self):
        self.tmp_dir = tempfile.mkdtemp()

    def rm_tmp_dir(self):
        shutil.rmtree(self.tmp_dir)

    def test_usr_bin_env(self):
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=True,
                              sh_bin='bash -e')

        runner._add_bootstrap_files_for_upload()

        self.assertIsNotNone(runner._master_bootstrap_script_path)
        self.assertTrue(os.path.exists(runner._master_bootstrap_script_path))

        lines = [line.rstrip() for line in
                 open(runner._master_bootstrap_script_path)]

        self.assertEqual(lines[0], '#!/usr/bin/env bash -e')

    def test_create_master_bootstrap_script(self):
        # create a fake src tarball
        foo_py_path = os.path.join(self.tmp_dir, 'foo.py')
        with open(foo_py_path, 'w'):
            pass

        yelpy_tar_gz_path = os.path.join(self.tmp_dir, 'yelpy.tar.gz')
        tar_and_gzip(self.tmp_dir, yelpy_tar_gz_path, prefix='yelpy')

        # use all the bootstrap options
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap=['python ' + foo_py_path + '#bar.py',
                                         's3://walrus/scripts/ohnoes.sh#'],
                              bootstrap_cmds=['echo "Hi!"', 'true', 'ls'],
                              bootstrap_files=['/tmp/quz'],
                              bootstrap_mrjob=True,
                              bootstrap_python_packages=[yelpy_tar_gz_path],
                              bootstrap_scripts=['speedups.sh', '/tmp/s.sh'])

        runner._add_bootstrap_files_for_upload()

        self.assertIsNotNone(runner._master_bootstrap_script_path)
        self.assertTrue(os.path.exists(runner._master_bootstrap_script_path))

        lines = [line.rstrip() for line in
                 open(runner._master_bootstrap_script_path)]

        self.assertEqual(lines[0], '#!/bin/sh -ex')

        # check PWD gets stored
        self.assertIn('__mrjob_PWD=$PWD', lines)

        def assertScriptDownloads(path, name=None):
            uri = runner._upload_mgr.uri(path)
            name = runner._bootstrap_dir_mgr.name('file', path, name=name)

            self.assertIn(
                'hadoop fs -copyToLocal %s $__mrjob_PWD/%s' % (uri, name),
                lines)
            self.assertIn(
                'chmod a+x $__mrjob_PWD/%s' % (name,),
                lines)

        # check files get downloaded
        assertScriptDownloads(foo_py_path, 'bar.py')
        assertScriptDownloads('s3://walrus/scripts/ohnoes.sh')
        assertScriptDownloads('/tmp/quz', 'quz')
        assertScriptDownloads(runner._mrjob_tar_gz_path)
        assertScriptDownloads(yelpy_tar_gz_path)
        assertScriptDownloads('speedups.sh')
        assertScriptDownloads('/tmp/s.sh')

        # check scripts get run

        # bootstrap
        self.assertIn('python $__mrjob_PWD/bar.py', lines)
        self.assertIn('$__mrjob_PWD/ohnoes.sh', lines)
        # bootstrap_cmds
        self.assertIn('echo "Hi!"', lines)
        self.assertIn('true', lines)
        self.assertIn('ls', lines)
        # bootstrap_mrjob
        mrjob_tar_gz_name = runner._bootstrap_dir_mgr.name(
            'file', runner._mrjob_tar_gz_path)
        self.assertIn("__mrjob_PYTHON_LIB=$(python -c 'from"
                      " distutils.sysconfig import get_python_lib; print"
                      " get_python_lib()')", lines)
        self.assertIn('sudo tar xfz $__mrjob_PWD/' + mrjob_tar_gz_name +
                      ' -C $__mrjob_PYTHON_LIB', lines)
        self.assertIn('sudo python -m compileall -f $__mrjob_PYTHON_LIB/mrjob'
                      ' && true', lines)
        # bootstrap_python_packages
        self.assertIn('sudo apt-get install -y python-pip || '
                'sudo yum install -y python-pip', lines)
        self.assertIn('sudo pip install $__mrjob_PWD/yelpy.tar.gz', lines)
        # bootstrap_scripts
        self.assertIn('$__mrjob_PWD/speedups.sh', lines)
        self.assertIn('$__mrjob_PWD/s.sh', lines)

    def test_no_bootstrap_script_if_not_needed(self):
        runner = EMRJobRunner(conf_paths=[], bootstrap_mrjob=False)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

        # bootstrap actions don't figure into the master bootstrap script
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=False,
                              bootstrap_actions=['foo', 'bar baz'],
                              pool_emr_job_flows=False)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

        # using pooling doesn't require us to create a bootstrap script
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=False,
                              pool_emr_job_flows=True)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

    def test_bootstrap_actions_get_added(self):
        bootstrap_actions = [
            ('s3://elasticmapreduce/bootstrap-actions/configure-hadoop'
             ' -m,mapred.tasktracker.map.tasks.maximum=1'),
            's3://foo/bar',
        ]

        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_actions=bootstrap_actions,
                              s3_sync_wait_time=0.00)

        job_flow_id = runner.make_persistent_job_flow()

        emr_conn = runner.make_emr_conn()
        job_flow = emr_conn.describe_jobflow(job_flow_id)
        actions = job_flow.bootstrapactions

        self.assertEqual(len(actions), 3)

        self.assertEqual(
            actions[0].path,
            's3://elasticmapreduce/bootstrap-actions/configure-hadoop')
        self.assertEqual(
            actions[0].args[0].value,
            '-m,mapred.tasktracker.map.tasks.maximum=1')
        self.assertEqual(actions[0].name, 'action 0')

        self.assertEqual(actions[1].path, 's3://foo/bar')
        self.assertEqual(actions[1].args, [])
        self.assertEqual(actions[1].name, 'action 1')

        # check for master bootstrap script
        self.assertTrue(actions[2].path.startswith('s3://mrjob-'))
        self.assertTrue(actions[2].path.endswith('b.py'))
        self.assertEqual(actions[2].args, [])
        self.assertEqual(actions[2].name, 'master')

        # make sure master bootstrap script is on S3
        self.assertTrue(runner.path_exists(actions[2].path))

    def test_bootstrap_mrjob_uses_python_bin(self):
        # use all the bootstrap options
        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_mrjob=True,
                              python_bin=['anaconda'])

        runner._add_bootstrap_files_for_upload()
        self.assertIsNotNone(runner._master_bootstrap_script_path)
        with open(runner._master_bootstrap_script_path, 'r') as f:
            content = f.read()

        self.assertIn('sudo anaconda -m compileall -f', content)

    def test_local_bootstrap_action(self):
        # make sure that local bootstrap action scripts get uploaded to S3
        action_path = os.path.join(self.tmp_dir, 'apt-install.sh')
        with open(action_path, 'w') as f:
            f.write('for $pkg in $@; do sudo apt-get install $pkg; done\n')

        bootstrap_actions = [
            action_path + ' python-scipy mysql-server']

        runner = EMRJobRunner(conf_paths=[],
                              bootstrap_actions=bootstrap_actions,
                              s3_sync_wait_time=0.00)

        job_flow_id = runner.make_persistent_job_flow()

        emr_conn = runner.make_emr_conn()
        job_flow = emr_conn.describe_jobflow(job_flow_id)
        actions = job_flow.bootstrapactions

        self.assertEqual(len(actions), 2)

        self.assertTrue(actions[0].path.startswith('s3://mrjob-'))
        self.assertTrue(actions[0].path.endswith('/apt-install.sh'))
        self.assertEqual(actions[0].name, 'action 0')
        self.assertEqual(actions[0].args[0].value, 'python-scipy')
        self.assertEqual(actions[0].args[1].value, 'mysql-server')

        # check for master boostrap script
        self.assertTrue(actions[1].path.startswith('s3://mrjob-'))
        self.assertTrue(actions[1].path.endswith('b.py'))
        self.assertEqual(actions[1].args, [])
        self.assertEqual(actions[1].name, 'master')

        # make sure master bootstrap script is on S3
        self.assertTrue(runner.path_exists(actions[1].path))


class EMRNoMapperTest(MockEMRAndS3TestCase):

    def setUp(self):
        super(EMRNoMapperTest, self).setUp()
        self.make_tmp_dir()

    def tearDown(self):
        super(EMRNoMapperTest, self).tearDown()
        self.rm_tmp_dir()

    def make_tmp_dir(self):
        self.tmp_dir = tempfile.mkdtemp()

    def rm_tmp_dir(self):
        shutil.rmtree(self.tmp_dir)

    def test_no_mapper(self):
        # read from STDIN, a local file, and a remote file
        stdin = StringIO('foo\nbar\n')

        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as local_input_file:
            local_input_file.write('bar\nqux\n')

        remote_input_path = 's3://walrus/data/foo'
        self.add_mock_s3_data({'walrus': {'data/foo': 'foo\n'}})

        # setup fake output
        self.mock_emr_output = {('j-MOCKCLUSTER0', 1): [
            '1\t"qux"\n2\t"bar"\n', '2\t"foo"\n5\tnull\n']}

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '-', local_input_path, remote_input_path])
        mr_job.sandbox(stdin=stdin)

        results = []

        with mr_job.make_runner() as runner:
            runner.run()

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

        self.assertEqual(sorted(results),
                         [(1, 'qux'), (2, 'bar'), (2, 'foo'), (5, None)])


class PoolMatchingTestCase(MockEMRAndS3TestCase):

    def make_pooled_job_flow(self, name=None, minutes_ago=0, **kwargs):
        """Returns ``(runner, job_flow_id)``. Set minutes_ago to set
        ``jobflow.startdatetime`` to seconds before
        ``datetime.datetime.now()``."""
        runner = EMRJobRunner(pool_emr_job_flows=True,
                              emr_job_flow_pool_name=name,
                              **kwargs)
        job_flow_id = runner.make_persistent_job_flow()
        jf = runner.make_emr_conn().describe_jobflow(job_flow_id)
        jf.state = 'WAITING'
        start = datetime.now() - timedelta(minutes=minutes_ago)
        jf.startdatetime = start.strftime(boto.utils.ISO8601)
        return runner, job_flow_id

    def get_job_flow(self, job_args, job_class=MRTwoStepJob):
        mr_job = job_class(job_args)
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.prepare_runner_for_ssh(runner)
            runner.run()

            job_flow_id = runner.get_cluster_id()

        return job_flow_id

    def assertJoins(self, job_flow_id, job_args, job_class=MRTwoStepJob):
        actual_job_flow_id = self.get_job_flow(job_args, job_class=job_class)

        self.assertEqual(actual_job_flow_id, job_flow_id)

    def assertDoesNotJoin(self, job_flow_id, job_args, job_class=MRTwoStepJob):

        actual_job_flow_id = self.get_job_flow(job_args, job_class=job_class)

        self.assertNotEqual(actual_job_flow_id, job_flow_id)

        # terminate the job flow created by this assert, to avoid
        # very confusing behavior (see Issue #331)
        emr_conn = EMRJobRunner(conf_paths=[]).make_emr_conn()
        emr_conn.terminate_jobflow(actual_job_flow_id)

    def make_simple_runner(self, pool_name):
        """Make an EMRJobRunner that is ready to try to find a pool to join"""
        mr_job = MRTwoStepJob([
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', pool_name])
        mr_job.sandbox()
        runner = mr_job.make_runner()
        self.prepare_runner_for_ssh(runner)
        runner._prepare_for_launch()
        return runner

    def test_make_new_pooled_job_flow(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '-v', '--pool-emr-job-flows'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.prepare_runner_for_ssh(runner)
            runner.run()

            # Make sure that the runner made a pooling-enabled job flow
            emr_conn = runner.make_emr_conn()
            job_flow_id = runner.get_cluster_id()
            job_flow = emr_conn.describe_jobflow(job_flow_id)
            jf_hash, jf_name = pool_hash_and_name(job_flow)
            self.assertEqual(jf_hash, runner._pool_hash())
            self.assertEqual(jf_name, runner._opts['emr_job_flow_pool_name'])
            self.assertEqual(job_flow.state, 'WAITING')

    def test_join_pooled_job_flow(self):
        _, job_flow_id = self.make_pooled_job_flow()

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows'])

    def test_join_named_pool(self):
        _, job_flow_id = self.make_pooled_job_flow('pool1')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'pool1'])

    def test_pooling_with_hadoop_version(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ami_version='1.0', hadoop_version='0.18')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--hadoop-version', '0.18', '--ami-version', '1.0'])

    def test_dont_join_pool_with_wrong_hadoop_version(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ami_version='1.0', hadoop_version='0.18')

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--hadoop-version', '0.20', '--ami-version', '1.0'])

    def test_join_anyway_if_i_say_so(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ami_version='1.0', hadoop_version='0.18')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--emr-job-flow-id', job_flow_id,
            '--hadoop-version', '0.20', '--ami-version', '1.0'])

    def test_pooling_with_ami_version(self):
        _, job_flow_id = self.make_pooled_job_flow(ami_version='2.0')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2.0'])

    def test_pooling_with_ami_version_prefix_major_minor(self):
        _, job_flow_id = self.make_pooled_job_flow(ami_version='2.0.0')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2.0'])

    def test_pooling_with_ami_version_prefix_major(self):
        _, job_flow_id = self.make_pooled_job_flow(ami_version='2.0.0')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '2'])

    def test_dont_join_pool_with_wrong_ami_version(self):
        _, job_flow_id = self.make_pooled_job_flow(ami_version='2.0')

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ami-version', '1.0'])

    def test_pooling_with_additional_emr_info(self):
        info = '{"tomatoes": "actually a fruit!"}'
        _, job_flow_id = self.make_pooled_job_flow(
            additional_emr_info=info)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--additional-emr-info', info])

    def test_dont_join_pool_with_wrong_additional_emr_info(self):
        info = '{"tomatoes": "actually a fruit!"}'
        _, job_flow_id = self.make_pooled_job_flow()

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--additional-emr-info', info])

    def test_join_pool_with_same_instance_type_and_count(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=20)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm2.4xlarge',
            '--num-ec2-instances', '20'])

    def test_join_pool_with_more_of_same_instance_type(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=20)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm2.4xlarge',
            '--num-ec2-instances', '5'])

    def test_join_job_flow_with_bigger_instances(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=20)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm1.small',
            '--num-ec2-instances', '20'])

    def test_join_job_flow_with_enough_cpu_and_memory(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='c1.xlarge',
            num_ec2_instances=3)

        # join the pooled job flow even though it has less instances total,
        # since they're have enough memory and CPU
        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm1.small',
            '--num-ec2-instances', '10'])

    def test_dont_join_job_flow_with_instances_with_too_little_memory(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='c1.xlarge',
            num_ec2_instances=20)

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'm2.4xlarge',
            '--num-ec2-instances', '2'])

    def test_master_instance_has_to_be_big_enough(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='c1.xlarge',
            num_ec2_instances=10)

        # We implicitly want a MASTER instance with c1.xlarge. The pooled
        # job flow has an m1.small master instance and 9 c1.xlarge core
        # instances, which doesn't match.
        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'c1.xlarge',
            '--num-ec2-instances', '1'])

    def test_unknown_instance_type_against_matching_pool(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='a1.sauce',
            num_ec2_instances=10)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '10'])

    def test_unknown_instance_type_against_pool_with_more_instances(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='a1.sauce',
            num_ec2_instances=20)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '10'])

    def test_unknown_instance_type_against_pool_with_less_instances(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='a1.sauce',
            num_ec2_instances=5)

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '10'])

    def test_unknown_instance_type_against_other_instance_types(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_instance_type='m2.4xlarge',
            num_ec2_instances=100)

        # for all we know, "a1.sauce" instances have even more memory and CPU
        # than m2.4xlarge
        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-instance-type', 'a1.sauce',
            '--num-ec2-instances', '2'])

    def test_can_join_job_flow_with_same_bid_price(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_master_instance_bid_price='0.25')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '0.25'])

    def test_can_join_job_flow_with_higher_bid_price(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_master_instance_bid_price='25.00')

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '0.25'])

    def test_cant_join_job_flow_with_lower_bid_price(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_master_instance_bid_price='0.25',
            num_ec2_instances=100)

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '25.00'])

    def test_on_demand_satisfies_any_bid_price(self):
        _, job_flow_id = self.make_pooled_job_flow()

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--ec2-master-instance-bid-price', '25.00'])

    def test_no_bid_price_satisfies_on_demand(self):
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_master_instance_bid_price='25.00')

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows'])

    def test_core_and_task_instance_types(self):
        # a tricky test that mixes and matches different criteria
        _, job_flow_id = self.make_pooled_job_flow(
            ec2_core_instance_bid_price='0.25',
            ec2_task_instance_bid_price='25.00',
            ec2_task_instance_type='c1.xlarge',
            num_ec2_core_instances=2,
            num_ec2_task_instances=3)

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--num-ec2-core-instances', '2',
            '--num-ec2-task-instances', '10',  # more instances, but smaller
            '--ec2-core-instance-bid-price', '0.10',
            '--ec2-master-instance-bid-price', '77.77',
            '--ec2-task-instance-bid-price', '22.00'])

    def test_dont_join_full_job_flow(self):
        dummy_runner, job_flow_id = self.make_pooled_job_flow('pool1')

        # fill the job flow
        self.mock_emr_clusters[job_flow_id].steps = 255 * [
            MockEmrObject(
                state='COMPLETED',
                name='dummy',
                actiononfailure='CANCEL_AND_WAIT',
                args=[])]

        # a two-step job shouldn't fit
        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'pool1'],
            job_class=MRTwoStepJob)

    def test_join_almost_full_job_flow(self):
        dummy_runner, job_flow_id = self.make_pooled_job_flow('pool1')

        # fill the job flow
        self.mock_emr_clusters[job_flow_id].steps = 255 * [
            MockEmrObject(
                state='COMPLETED',
                name='dummy',
                actiononfailure='CANCEL_AND_WAIT',
                enddatetime='definitely not none',
                jar='/stuff/hadoop-streaming.jar',
                args=[])]

        # a one-step job should fit
        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'pool1'],
            job_class=MRWordCount)

    def test_dont_join_idle_with_pending_steps(self):
        dummy_runner, job_flow_id = self.make_pooled_job_flow()

        self.mock_emr_clusters[job_flow_id].steps = [
            MockEmrObject(
                state='PENDING',
                mock_no_progress=True,
                name='dummy',
                actiononfailure='CANCEL_AND_WAIT',
                args=[])]

        self.assertDoesNotJoin(job_flow_id,
                               ['-r', 'emr', '--pool-emr-job-flows'])

    def test_do_join_idle_with_cancelled_steps(self):
        dummy_runner, job_flow_id = self.make_pooled_job_flow()

        self.mock_emr_clusters[job_flow_id].steps = [
            MockEmrObject(
                state='FAILED',
                name='step 1 of 2',
                actiononfailure='CANCEL_AND_WAIT',
                enddatetime='sometime in the past',
                args=[]),
            # step 2 never ran, so its enddatetime is not set
            MockEmrObject(
                state='CANCELLED',
                name='step 2 of 2',
                actiononfailure='CANCEL_AND_WAIT',
                args=[])
        ]

        self.assertJoins(job_flow_id,
                         ['-r', 'emr', '--pool-emr-job-flows'])

    def test_dont_join_wrong_named_pool(self):
        _, job_flow_id = self.make_pooled_job_flow('pool1')

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--pool-name', 'not_pool1'])

    def test_dont_join_wrong_mrjob_version(self):
        _, job_flow_id = self.make_pooled_job_flow('pool1')

        old_version = mrjob.__version__

        try:
            mrjob.__version__ = 'OVER NINE THOUSAAAAAND'

            self.assertDoesNotJoin(job_flow_id, [
                '-r', 'emr', '-v', '--pool-emr-job-flows',
                '--pool-name', 'not_pool1'])
        finally:
            mrjob.__version__ = old_version

    def test_join_similarly_bootstrapped_pool(self):
        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as input_file:
            input_file.write('bar\nfoo\n')

        _, job_flow_id = self.make_pooled_job_flow(
            bootstrap_files=[local_input_path])

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--bootstrap-file', local_input_path])

    def test_dont_join_differently_bootstrapped_pool(self):
        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as input_file:
            input_file.write('bar\nfoo\n')

        _, job_flow_id = self.make_pooled_job_flow()

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--bootstrap-file', local_input_path])

    def test_dont_join_differently_bootstrapped_pool_2(self):
        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'w') as input_file:
            input_file.write('bar\nfoo\n')

        bootstrap_path = os.path.join(self.tmp_dir, 'go.sh')
        with open(bootstrap_path, 'w') as f:
            f.write('#!/usr/bin/sh\necho "hi mom"\n')

        _, job_flow_id = self.make_pooled_job_flow()

        self.assertDoesNotJoin(job_flow_id, [
            '-r', 'emr', '-v', '--pool-emr-job-flows',
            '--bootstrap-action', bootstrap_path + ' a b c'])

    def test_pool_contention(self):
        _, job_flow_id = self.make_pooled_job_flow('robert_downey_jr')

        def runner_plz():
            mr_job = MRTwoStepJob([
                '-r', 'emr', '-v', '--pool-emr-job-flows',
                '--pool-name', 'robert_downey_jr'])
            mr_job.sandbox()
            runner = mr_job.make_runner()
            runner._prepare_for_launch()
            return runner

        runner1 = runner_plz()
        runner2 = runner_plz()

        jf1 = runner1.find_job_flow()
        jf2 = runner2.find_job_flow()
        self.assertEqual(jf1.jobflowid, job_flow_id)
        self.assertEqual(jf2, None)
        jf1.status = 'COMPLETED'

    def test_sorting_by_time(self):
        _, job_flow_id_1 = self.make_pooled_job_flow('pool1', minutes_ago=20)
        _, job_flow_id_2 = self.make_pooled_job_flow('pool1', minutes_ago=40)

        runner1 = self.make_simple_runner('pool1')
        runner2 = self.make_simple_runner('pool1')

        jf1 = runner1.find_job_flow()
        jf2 = runner2.find_job_flow()
        self.assertEqual(jf1.jobflowid, job_flow_id_1)
        self.assertEqual(jf2.jobflowid, job_flow_id_2)
        jf1.status = 'COMPLETED'
        jf2.status = 'COMPLETED'

    def test_sorting_by_cpu_hours(self):
        _, job_flow_id_1 = self.make_pooled_job_flow('pool1',
                                                     minutes_ago=40,
                                                     num_ec2_instances=2)
        _, job_flow_id_2 = self.make_pooled_job_flow('pool1',
                                                     minutes_ago=20,
                                                     num_ec2_instances=1)

        runner1 = self.make_simple_runner('pool1')
        runner2 = self.make_simple_runner('pool1')

        jf1 = runner1.find_job_flow()
        jf2 = runner2.find_job_flow()
        self.assertEqual(jf1.jobflowid, job_flow_id_1)
        self.assertEqual(jf2.jobflowid, job_flow_id_2)
        jf1.status = 'COMPLETED'
        jf2.status = 'COMPLETED'

    def test_dont_destroy_own_pooled_job_flow_on_failure(self):
        # Issue 242: job failure shouldn't kill the pooled job flows
        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--pool-emr-job-flow'])
        mr_job.sandbox()

        self.mock_emr_failures = {('j-MOCKCLUSTER0', 0): None}

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.emr'):
                self.assertRaises(Exception, runner.run)

            emr_conn = runner.make_emr_conn()
            job_flow_id = runner.get_cluster_id()
            for _ in xrange(10):
                emr_conn.simulate_progress(job_flow_id)

            job_flow = emr_conn.describe_jobflow(job_flow_id)
            self.assertEqual(job_flow.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        emr_conn = runner.make_emr_conn()
        job_flow_id = runner.get_cluster_id()
        for _ in xrange(10):
            emr_conn.simulate_progress(job_flow_id)

        job_flow = emr_conn.describe_jobflow(job_flow_id)
        self.assertEqual(job_flow.state, 'WAITING')

    def test_dont_destroy_other_pooled_job_flow_on_failure(self):
        # Issue 242: job failure shouldn't kill the pooled job flows
        _, job_flow_id = self.make_pooled_job_flow()

        self.mock_emr_failures = {(job_flow_id, 0): None}

        mr_job = MRTwoStepJob(['-r', 'emr', '-v',
                               '--pool-emr-job-flow'])
        mr_job.sandbox()

        self.mock_emr_failures = {('j-MOCKCLUSTER0', 0): None}

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, EMRJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.emr'):
                self.assertRaises(Exception, runner.run)

            self.assertEqual(runner.get_cluster_id(), job_flow_id)

            emr_conn = runner.make_emr_conn()
            for _ in xrange(10):
                emr_conn.simulate_progress(job_flow_id)

            job_flow = emr_conn.describe_jobflow(job_flow_id)
            self.assertEqual(job_flow.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        emr_conn = runner.make_emr_conn()
        job_flow_id = runner.get_cluster_id()
        for _ in xrange(10):
            emr_conn.simulate_progress(job_flow_id)

        job_flow = emr_conn.describe_jobflow(job_flow_id)
        self.assertEqual(job_flow.state, 'WAITING')

    def test_max_hours_idle_doesnt_affect_pool_hash(self):
        # max_hours_idle uses a bootstrap action, but it's not included
        # in the pool hash
        _, job_flow_id = self.make_pooled_job_flow()

        self.assertJoins(job_flow_id, [
            '-r', 'emr', '--pool-emr-job-flows', '--max-hours-idle', '1'])

    def test_can_join_job_flow_started_with_max_hours_idle(self):
        _, job_flow_id = self.make_pooled_job_flow(max_hours_idle=1)

        self.assertJoins(job_flow_id, ['-r', 'emr', '--pool-emr-job-flows'])


class PoolingDisablingTestCase(MockEMRAndS3TestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
        'pool_emr_job_flows': True,
    }}}

    def test_can_turn_off_pooling_from_cmd_line(self):
        mr_job = MRTwoStepJob(['-r', 'emr', '-v', '--no-pool-emr-job-flows'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.prepare_runner_for_ssh(runner)
            runner.run()

            job_flow_id = runner.get_cluster_id()
            jf = runner.make_emr_conn().describe_jobflow(job_flow_id)
            self.assertEqual(jf.keepjobflowalivewhennosteps, 'false')


class S3LockTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(S3LockTestCase, self).setUp()
        self.make_buckets()

    def make_buckets(self):
        self.add_mock_s3_data({'locks': {
            'expired_lock': 'x',
        }}, datetime.utcnow() - timedelta(minutes=30))
        self.lock_uri = 's3://locks/some_lock'
        self.expired_lock_uri = 's3://locks/expired_lock'

    def test_lock(self):
        # Most basic test case
        runner = EMRJobRunner(conf_paths=[])
        s3_conn = runner.make_s3_conn()

        self.assertEqual(
            True, attempt_to_acquire_lock(s3_conn, self.lock_uri, 0, 'jf1'))

        self.assertEqual(
            False, attempt_to_acquire_lock(s3_conn, self.lock_uri, 0, 'jf2'))

    def test_lock_expiration(self):
        runner = EMRJobRunner(conf_paths=[])
        s3_conn = runner.make_s3_conn()
        did_lock = attempt_to_acquire_lock(
            s3_conn, self.expired_lock_uri, 0, 'jf1',
            mins_to_expiration=5)
        self.assertEqual(True, did_lock)

    def test_key_race_condition(self):
        # Test case where one attempt puts the key in existence
        runner = EMRJobRunner(conf_paths=[])
        s3_conn = runner.make_s3_conn()

        key = _lock_acquire_step_1(s3_conn, self.lock_uri, 'jf1')
        self.assertNotEqual(key, None)

        key2 = _lock_acquire_step_1(s3_conn, self.lock_uri, 'jf2')
        self.assertEqual(key2, None)

    def test_read_race_condition(self):
        # test case where both try to create the key
        runner = EMRJobRunner(conf_paths=[])
        s3_conn = runner.make_s3_conn()

        key = _lock_acquire_step_1(s3_conn, self.lock_uri, 'jf1')
        self.assertNotEqual(key, None)

        # acquire the key by subversive means to simulate contention
        bucket_name, key_prefix = parse_s3_uri(self.lock_uri)
        bucket = s3_conn.get_bucket(bucket_name)
        key2 = bucket.get_key(key_prefix)

        # and take the lock!
        key2.set_contents_from_string('jf2')

        self.assertFalse(_lock_acquire_step_2(key, 'jf1'), 'Lock should fail')


class MaxHoursIdleTestCase(MockEMRAndS3TestCase):

    def assertRanIdleTimeoutScriptWith(self, runner, args):
        emr_conn = runner.make_emr_conn()
        job_flow = emr_conn.describe_jobflow(runner.get_cluster_id())
        action = job_flow.bootstrapactions[-1]
        self.assertEqual(action.name, 'idle timeout')
        self.assertEqual(
            action.path,
            runner._upload_mgr.uri(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH))
        self.assertEqual([arg.value for arg in action.args], args)

    def assertDidNotUseIdleTimeoutScript(self, runner):
        emr_conn = runner.make_emr_conn()
        job_flow = emr_conn.describe_jobflow(runner.get_cluster_id())
        action_names = [ba.name for ba in job_flow.bootstrapactions]
        self.assertNotIn('idle timeout', action_names)
        # idle timeout script should not even be uploaded
        self.assertNotIn(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH,
                         runner._upload_mgr.path_to_uri())

    def test_default(self):
        mr_job = MRWordCount(['-r', 'emr'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_non_persistent_job_flow(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '1'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_persistent_job_flow(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '0.01'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertRanIdleTimeoutScriptWith(runner, ['36', '300'])

    def test_mins_to_end_of_hour(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '1',
                              '--mins-to-end-of-hour', '10'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertRanIdleTimeoutScriptWith(runner, ['3600', '600'])

    def test_mins_to_end_of_hour_does_nothing_without_max_hours_idle(self):
        mr_job = MRWordCount(['-r', 'emr', '--mins-to-end-of-hour', '10'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_use_integers(self):
        mr_job = MRWordCount(['-r', 'emr', '--max-hours-idle', '1.000001',
                              '--mins-to-end-of-hour', '10.000001'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_job_flow()
            self.assertRanIdleTimeoutScriptWith(runner, ['3600', '600'])

    def pooled_job_flows(self):
        mr_job = MRWordCount(['-r', 'emr', '--pool-emr-job-flows',
                              '--max-hours-idle', '0.5'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertRanIdleTimeoutScriptWith(runner, ['1800', '300'])

    def test_bootstrap_script_is_actually_installed(self):
        self.assertTrue(os.path.exists(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH))

class TestCatFallback(MockEMRAndS3TestCase):

    def test_s3_cat(self):
        self.add_mock_s3_data(
            {'walrus': {'one': 'one_text',
                        'two': 'two_text',
                        'three': 'three_text'}})

        runner = EMRJobRunner(s3_scratch_uri='s3://walrus/tmp',
                              conf_paths=[])

        self.assertEqual(list(runner.cat('s3://walrus/one')), ['one_text\n'])

    def test_ssh_cat(self):
        runner = EMRJobRunner(conf_paths=[])
        self.prepare_runner_for_ssh(runner)
        mock_ssh_file('testmaster', 'etc/init.d', 'meow')

        ssh_cat_gen = runner.cat(
            SSH_PREFIX + runner._address + '/etc/init.d')
        self.assertEqual(list(ssh_cat_gen)[0].rstrip(), 'meow')
        self.assertRaises(
            IOError, list,
            runner.cat(SSH_PREFIX + runner._address + '/does_not_exist'))

    def test_ssh_cat_errlog(self):
        # A file *containing* an error message shouldn't cause an error.
        runner = EMRJobRunner(conf_paths=[])
        self.prepare_runner_for_ssh(runner)

        error_message = 'cat: logs/err.log: No such file or directory\n'
        mock_ssh_file('testmaster', 'logs/err.log', error_message)
        self.assertEqual(
            list(runner.cat(SSH_PREFIX + runner._address + '/logs/err.log')),
            [error_message])


class CleanUpJobTestCase(MockEMRAndS3TestCase):

    @contextmanager
    def _test_mode(self, mode):
        r = EMRJobRunner(conf_paths=[])
        with nested(
            patch.object(r, '_cleanup_local_scratch'),
            patch.object(r, '_cleanup_remote_scratch'),
            patch.object(r, '_cleanup_logs'),
            patch.object(r, '_cleanup_job'),
            patch.object(r, '_cleanup_job_flow')) as mocks:
            r.cleanup(mode=mode)
            yield mocks

    def _quick_runner(self):
        r = EMRJobRunner(conf_paths=[])
        r._cluster_id = 'j-ESSEOWENS'
        r._address = 'Albuquerque, NM'
        r._ran_job = False
        return r

    def test_cleanup_all(self):
        with self._test_mode('ALL') as (
                m_local_scratch,
                m_remote_scratch,
                m_logs,
                m_jobs,
                m_job_flows):
            self.assertFalse(m_job_flows.called)
            self.assertFalse(m_jobs.called)
            self.assertTrue(m_local_scratch.called)
            self.assertTrue(m_remote_scratch.called)
            self.assertTrue(m_logs.called)

    def test_cleanup_job(self):
        with self._test_mode('JOB') as (
                m_local_scratch,
                m_remote_scratch,
                m_logs,
                m_jobs,
                m_job_flows):
            self.assertFalse(m_local_scratch.called)
            self.assertFalse(m_remote_scratch.called)
            self.assertFalse(m_logs.called)
            self.assertFalse(m_job_flows.called)
            self.assertFalse(m_jobs.called)  # Only will trigger on failure

    def test_cleanup_none(self):
        with self._test_mode('NONE') as (
                m_local_scratch,
                m_remote_scratch,
                m_logs,
                m_jobs,
                m_job_flows):
            self.assertFalse(m_local_scratch.called)
            self.assertFalse(m_remote_scratch.called)
            self.assertFalse(m_logs.called)
            self.assertFalse(m_jobs.called)
            self.assertFalse(m_job_flows.called)

    def test_job_cleanup_mechanics_succeed(self):
        with no_handlers_for_logger():
            r = self._quick_runner()
            with patch.object(mrjob.emr, 'ssh_terminate_single_job') as m:
                r._cleanup_job()
            self.assertTrue(m.called)
            m.assert_any_call(['ssh'], 'Albuquerque, NM', None)

    def test_job_cleanup_mechanics_ssh_fail(self):
        def die_ssh(*args, **kwargs):
            raise IOError

        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            stderr = StringIO()
            log_to_stream('mrjob.emr', stderr)
            with patch.object(mrjob.emr, 'ssh_terminate_single_job',
                              side_effect=die_ssh):
                r._cleanup_job()
                self.assertIn('Unable to kill job', stderr.getvalue())

    def test_job_cleanup_mechanics_io_fail(self):
        def die_io(*args, **kwargs):
            raise IOError

        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr, 'ssh_terminate_single_job',
                              side_effect=die_io):
                stderr = StringIO()
                log_to_stream('mrjob.emr', stderr)
                r._cleanup_job()
                self.assertIn('Unable to kill job', stderr.getvalue())

    def test_dont_kill_if_successful(self):
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr, 'ssh_terminate_single_job') as m:
                r._ran_job = True
                r._cleanup_job()
                m.assert_not_called()

    def test_kill_job_flow(self):
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr.EMRJobRunner, 'make_emr_conn') as m:
                r._cleanup_job_flow()
                self.assertTrue(m().terminate_jobflow.called)

    def test_kill_job_flow_if_successful(self):
        # If they are setting up the cleanup to kill the job flow, mrjob should
        # kill the job flow independent of job success.
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr.EMRJobRunner, 'make_emr_conn') as m:
                r._ran_job = True
                r._cleanup_job_flow()
                self.assertTrue(m().terminate_jobflow.called)

    def test_kill_persistent_job_flow(self):
        with no_handlers_for_logger('mrjob.emr'):
            r = self._quick_runner()
            with patch.object(mrjob.emr.EMRJobRunner, 'make_emr_conn') as m:
                r._opts['emr_job_flow_id'] = 'j-MOCKCLUSTER0'
                r._cleanup_job_flow()
                self.assertTrue(m().terminate_jobflow.called)


class JobWaitTestCase(MockEMRAndS3TestCase):

    # A list of job ids that hold booleans of whether or not the job can
    # acquire a lock. Helps simulate mrjob.emr.attempt_to_acquire_lock.
    JOB_ID_LOCKS = {
        'j-fail-lock': False,
        'j-successful-lock': True,
        'j-brown': True,
        'j-epic-fail-lock': False
    }

    def setUp(self):
        super(JobWaitTestCase, self).setUp()
        self.future_jobs = []
        self.jobs = []
        self.sleep_counter = 0

        def side_effect_lock_uri(*args):
            return args[0]  # Return the only arg given to it.

        def side_effect_acquire_lock(*args):
            job_id = args[1].jobflowid
            return self.JOB_ID_LOCKS[job_id]

        def side_effect_usable_job_flows(*args, **kwargs):
            return_jobs = []
            for job in self.jobs:
                if job.jobflowid not in kwargs['exclude']:
                    return_jobs.append(job)
            return return_jobs

        def side_effect_time_sleep(*args):
            self.sleep_counter += 1
            if len(self.future_jobs) > 0:
                future_job = self.future_jobs.pop(0)
                self.jobs.append(future_job)

        self.simple_patch(EMRJobRunner, 'make_emr_conn')
        self.simple_patch(S3Filesystem, 'make_s3_conn',
                          side_effect=self._mock_boto_connect_s3)
        self.simple_patch(EMRJobRunner, 'usable_job_flows',
            side_effect=side_effect_usable_job_flows)
        self.simple_patch(EMRJobRunner, '_lock_uri',
            side_effect=side_effect_lock_uri)
        self.simple_patch(mrjob.emr, 'attempt_to_acquire_lock',
            side_effect=side_effect_acquire_lock)
        self.simple_patch(time, 'sleep',
            side_effect=side_effect_time_sleep)

    def tearDown(self):
        super(JobWaitTestCase, self).tearDown()
        self.jobs = []
        self.future_jobs = []

    def add_job_flow(self, job_names, job_list):
        """Puts a fake job flow into a list of jobs for testing."""
        for name in job_names:
            jf = Mock()
            jf.state = 'WAITING'
            jf.jobflowid = name
            job_list.append(jf)

    def test_no_waiting_for_job_pool_fail(self):
        self.add_job_flow(['j-fail-lock'], self.jobs)
        runner = EMRJobRunner(conf_paths=[])
        runner._opts['pool_wait_minutes'] = 0
        result = runner.find_job_flow()
        self.assertEqual(result, None)
        self.assertEqual(self.sleep_counter, 0)

    def test_no_waiting_for_job_pool_success(self):
        self.add_job_flow(['j-fail-lock'], self.jobs)
        runner = EMRJobRunner(conf_paths=[])
        runner._opts['pool_wait_minutes'] = 0
        result = runner.find_job_flow()
        self.assertEqual(result, None)

    def test_acquire_lock_on_first_attempt(self):
        self.add_job_flow(['j-successful-lock'], self.jobs)
        runner = EMRJobRunner(conf_paths=[])
        runner._opts['pool_wait_minutes'] = 1
        result = runner.find_job_flow()
        self.assertEqual(result.jobflowid, 'j-successful-lock')
        self.assertEqual(self.sleep_counter, 0)

    def test_sleep_then_acquire_lock(self):
        self.add_job_flow(['j-fail-lock'], self.jobs)
        self.add_job_flow(['j-successful-lock'], self.future_jobs)
        runner = EMRJobRunner(conf_paths=[])
        runner._opts['pool_wait_minutes'] = 1
        result = runner.find_job_flow()
        self.assertEqual(result.jobflowid, 'j-successful-lock')
        self.assertEqual(self.sleep_counter, 1)

    def test_timeout_waiting_for_job_flow(self):
        self.add_job_flow(['j-fail-lock'], self.jobs)
        self.add_job_flow(['j-epic-fail-lock'], self.future_jobs)
        runner = EMRJobRunner(conf_paths=[])
        runner._opts['pool_wait_minutes'] = 1
        result = runner.find_job_flow()
        self.assertEqual(result, None)
        self.assertEqual(self.sleep_counter, 2)


class BuildStreamingStepTestCase(FastEMRTestCase):

    def setUp(self):
        super(BuildStreamingStepTestCase, self).setUp()
        with patch_fs_s3():
            self.runner = EMRJobRunner(
                mr_job_script='my_job.py', conf_paths=[], stdin=StringIO())
        self.runner._steps = []  # don't actually run `my_job.py --steps`
        self.runner._add_job_files_for_upload()

        self.simple_patch(
            self.runner, '_step_input_uris', return_value=['input'])
        self.simple_patch(
            self.runner, '_step_output_uri', return_value=['output'])
        self.simple_patch(
            self.runner, '_get_streaming_jar', return_value=['streaming.jar'])

        self.simple_patch(boto.emr, 'StreamingStep', dict)
        self.runner._hadoop_version = '0.20'

    def _assert_streaming_step(self, step, **kwargs):
        self.runner._steps = [step]
        d = self.runner._build_streaming_step(0)
        for k, v in kwargs.iteritems():
            self.assertEqual(d[k], v)

    def test_basic_mapper(self):
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'mapper': {
                    'type': 'script',
                },
            },
            mapper="python my_job.py --step-num=0 --mapper",
            reducer=None,
        )

    def test_basic_reducer(self):
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'reducer': {
                    'type': 'script',
                },
            },
            mapper="cat",
            reducer="python my_job.py --step-num=0 --reducer",
        )

    def test_pre_filters(self):
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'mapper': {
                    'type': 'script',
                    'pre_filter': 'grep anything',
                },
                'combiner': {
                    'type': 'script',
                    'pre_filter': 'grep nothing',
                },
                'reducer': {
                    'type': 'script',
                    'pre_filter': 'grep something',
                },
            },
            mapper=("bash -c 'grep anything | python my_job.py --step-num=0"
                    " --mapper'"),
            combiner=("bash -c 'grep nothing | python my_job.py --step-num=0"
                    " --combiner'"),
            reducer=("bash -c 'grep something | python my_job.py --step-num=0"
                    " --reducer'"),
        )

    def test_pre_filter_escaping(self):
        # ESCAPE ALL THE THINGS!!!
        self._assert_streaming_step(
            {
                'type': 'streaming',
                'mapper': {
                    'type': 'script',
                    'pre_filter': bash_wrap("grep 'anything'"),
                },
            },
            mapper=(
                "bash -c 'bash -c '\\''grep"
                " '\\''\\'\\'''\\''anything'\\''\\'\\'''\\'''\\'' |"
                " python my_job.py --step-num=0 --mapper'"),
        )


class JarStepTestCase(MockEMRAndS3TestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'emr': {
        'check_emr_status_every': 0.00,
        's3_sync_wait_time': 0.00,
    }}}

    def test_local_jar_gets_uploaded(self):
        fake_jar = os.path.join(self.tmp_dir, 'fake.jar')
        with open(fake_jar, 'w'):
            pass

        job = MRJustAJar(['-r', 'emr', '--jar', fake_jar])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            self.assertIn(fake_jar, runner._upload_mgr.path_to_uri())
            jar_uri = runner._upload_mgr.uri(fake_jar)
            self.assertTrue(runner.ls(jar_uri))

            emr_conn = runner.make_emr_conn()
            job_flow = emr_conn.describe_jobflow(runner.get_cluster_id())
            self.assertEqual(len(job_flow.steps), 1)
            self.assertEqual(job_flow.steps[0].jar, jar_uri)

    def test_jar_on_s3(self):
        self.add_mock_s3_data({'dubliners': {'whiskeyinthe.jar': ''}})
        JAR_URI = 's3://dubliners/whiskeyinthe.jar'

        job = MRJustAJar(['-r', 'emr', '--jar', JAR_URI])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            job_flow = emr_conn.describe_jobflow(runner.get_cluster_id())
            self.assertEqual(len(job_flow.steps), 1)
            self.assertEqual(job_flow.steps[0].jar, JAR_URI)

    def test_jar_inside_emr(self):
        job = MRJustAJar(['-r', 'emr', '--jar',
                          'file:///home/hadoop/hadoop-examples.jar'])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            job_flow = emr_conn.describe_jobflow(runner.get_cluster_id())
            self.assertEqual(len(job_flow.steps), 1)
            self.assertEqual(job_flow.steps[0].jar,
                             '/home/hadoop/hadoop-examples.jar')

    def test_input_output_interpolation(self):
        fake_jar = os.path.join(self.tmp_dir, 'fake.jar')
        open(fake_jar, 'w').close()
        input1 = os.path.join(self.tmp_dir, 'input1')
        open(input1, 'w').close()
        input2 = os.path.join(self.tmp_dir, 'input2')
        open(input2, 'w').close()

        job = MRJarAndStreaming(
            ['-r', 'emr', '--jar', fake_jar, input1, input2])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()

            emr_conn = runner.make_emr_conn()
            job_flow = emr_conn.describe_jobflow(runner.get_cluster_id())

            self.assertEqual(len(job_flow.steps), 2)
            jar_step, streaming_step = job_flow.steps

            # on EMR, the jar gets uploaded
            self.assertEqual(jar_step.jar, runner._upload_mgr.uri(fake_jar))

            jar_args = [arg.value for arg in jar_step.args]
            self.assertEqual(len(jar_args), 3)
            self.assertEqual(jar_args[0], 'stuff')

            # check input is interpolated
            input_arg = ','.join(
                runner._upload_mgr.uri(path) for path in (input1, input2))
            self.assertEqual(jar_args[1], input_arg)

            # check output of jar is input of next step
            jar_output_arg = jar_args[2]
            streaming_args = [arg.value for arg in streaming_step.args]
            streaming_input_arg = streaming_args[
                streaming_args.index('-input') + 1]
            self.assertEqual(jar_output_arg, streaming_input_arg)


class ActionOnFailureTestCase(MockEMRAndS3TestCase):

    def test_default(self):
        runner = EMRJobRunner()
        self.assertEqual(runner._action_on_failure,
                         'TERMINATE_CLUSTER')

    def test_default_with_job_flow_id(self):
        runner = EMRJobRunner(emr_job_flow_id='j-JOBFLOW')
        self.assertEqual(runner._action_on_failure,
                         'CANCEL_AND_WAIT')

    def test_default_with_pooling(self):
        runner = EMRJobRunner(pool_emr_job_flows=True)
        self.assertEqual(runner._action_on_failure,
                         'CANCEL_AND_WAIT')

    def test_option(self):
        runner = EMRJobRunner(emr_action_on_failure='CONTINUE')
        self.assertEqual(runner._action_on_failure,
                         'CONTINUE')

    def test_switch(self):
        mr_job = MRWordCount(
            ['-r', 'emr', '--emr-action-on-failure', 'CONTINUE'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            self.assertEqual(runner._action_on_failure, 'CONTINUE')


class MultiPartUploadTestCase(MockEMRAndS3TestCase):

    PART_SIZE_IN_MB = 50.0 / 1024 / 1024
    TEST_BUCKET = 'walrus'
    TEST_FILENAME = 'data.dat'
    TEST_S3_URI = 's3://%s/%s' % (TEST_BUCKET, TEST_FILENAME)

    def setUp(self):
        super(MultiPartUploadTestCase, self).setUp()
        # create the walrus bucket
        self.add_mock_s3_data({self.TEST_BUCKET: {}})

    def upload_data(self, runner, data):
        """Upload some bytes to S3"""
        data_path = os.path.join(self.tmp_dir, self.TEST_FILENAME)
        with open(data_path, 'w') as fp:
            fp.write(data)

        s3_conn = runner.make_s3_conn()

        runner._upload_contents(self.TEST_S3_URI, s3_conn, data_path)

    def assert_upload_succeeds(self, runner, data, expect_multipart):
        """Write the data to a temp file, and then upload it to (mock) S3,
        checking that the data successfully uploaded."""
        with patch.object(runner, '_upload_parts', wraps=runner._upload_parts):
            self.upload_data(runner, data)

            s3_key = runner.get_s3_key(self.TEST_S3_URI)
            self.assertEqual(s3_key.get_contents_as_string(), data)
            self.assertEqual(runner._upload_parts.called, expect_multipart)

    def test_small_file(self):
        runner = EMRJobRunner()
        data = 'beavers mate for life'

        self.assert_upload_succeeds(runner, data, expect_multipart=False)

    @unittest.skipIf(filechunkio is None, 'need filechunkio')
    def test_large_file(self):
        # Real S3 has a minimum chunk size of 5MB, but I'd rather not
        # store that in memory (in our mock S3 filesystem)
        runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
        self.assertEqual(runner._get_upload_part_size(), 50)

        data = 'Mew' * 20
        self.assert_upload_succeeds(runner, data, expect_multipart=True)

    def test_file_size_equals_part_size(self):
        runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
        self.assertEqual(runner._get_upload_part_size(), 50)

        data = 'o' * 50
        self.assert_upload_succeeds(runner, data, expect_multipart=False)

    def test_disable_multipart(self):
        runner = EMRJobRunner(s3_upload_part_size=0)
        self.assertEqual(runner._get_upload_part_size(), 0)

        data = 'Mew' * 20
        self.assert_upload_succeeds(runner, data, expect_multipart=False)

    def test_no_filechunkio(self):
        with patch.object(mrjob.emr, 'filechunkio', None):
            runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
            self.assertEqual(runner._get_upload_part_size(), 50)

            data = 'Mew' * 20
            with logger_disabled('mrjob.emr'):
                self.assert_upload_succeeds(runner, data,
                                            expect_multipart=False)

    def test_exception_while_uploading_large_file(self):

        runner = EMRJobRunner(s3_upload_part_size=self.PART_SIZE_IN_MB)
        self.assertEqual(runner._get_upload_part_size(), 50)

        data = 'Mew' * 20

        with patch.object(runner, '_upload_parts', side_effect=IOError):
            self.assertRaises(IOError, self.upload_data, runner, data)

            s3_key = runner.get_s3_key(self.TEST_S3_URI)
            self.assertTrue(s3_key.mock_multipart_upload_was_cancelled())


class SecurityTokenTestCase(MockEMRAndS3TestCase):

    def setUp(self):
        super(SecurityTokenTestCase, self).setUp()

        self.mock_emr = self.start(patch('boto.emr.connection.EmrConnection'))
        self.mock_iam = self.start(patch('boto.connect_iam'))

        # runner needs to do stuff with S3 on initialization
        self.mock_s3 = self.start(patch('boto.connect_s3',
                                        wraps=boto.connect_s3))

    def test_connections_without_security_token(self):
        runner = EMRJobRunner()

        runner.make_emr_conn()

        self.assertTrue(self.mock_emr.called)
        # security_token shouldn't even be in kwargs
        # (boto 2.2.0 doesn't allow it)
        emr_kwargs = self.mock_emr.call_args[1]
        self.assertNotIn('security_token', emr_kwargs)

        runner.make_iam_conn()

        self.assertTrue(self.mock_iam.called)
        # security_token shouldn't even be in kwargs
        # (boto 2.2.0 doesn't allow it)
        iam_kwargs = self.mock_iam.call_args[1]
        self.assertNotIn('security_token', iam_kwargs)

        runner.make_s3_conn()

        self.assertTrue(self.mock_s3.called)
        # S3 could accept security token, even in boto 2.2.0
        s3_kwargs = self.mock_s3.call_args[1]
        self.assertIn('security_token', s3_kwargs)
        self.assertEqual(s3_kwargs['security_token'], None)

    def test_connections_with_security_token(self):
        runner = EMRJobRunner(aws_security_token='meow')

        runner.make_emr_conn()

        self.assertTrue(self.mock_emr.called)
        emr_kwargs = self.mock_emr.call_args[1]
        self.assertIn('security_token', emr_kwargs)
        self.assertEqual(emr_kwargs['security_token'], 'meow')

        runner.make_iam_conn()

        self.assertTrue(self.mock_iam.called)
        iam_kwargs = self.mock_iam.call_args[1]
        self.assertIn('security_token', iam_kwargs)
        self.assertEqual(iam_kwargs['security_token'], 'meow')

        runner.make_s3_conn()

        self.assertTrue(self.mock_s3.called)
        s3_kwargs = self.mock_s3.call_args[1]
        self.assertIn('security_token', s3_kwargs)
        self.assertEqual(s3_kwargs['security_token'], 'meow')
