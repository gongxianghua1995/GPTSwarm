"""Pro input isolation, language handling and parser contracts (no API calls)."""
import unittest
from swarm.environment.agents.swe_bench.mini_benchmark import image_name, task_text, required_images
from swarm.environment.agents.swe_bench.mini_runtime import MiniRuntime, is_test_path
from swarm.environment.agents.swe_bench.mini_checks import check_evidence
from experiments.swebench_pro_spec import extract_test_checkout, parse_log_pro_pytest, parse_log_pro_gotest, parse_log_pro_jest, make_eval_script


class ProTests(unittest.TestCase):
    def test_jest_evidence_requires_actual_tests(self):
        command = 'npx --no-install jest a.test.ts --runInBand'
        self.assertEqual(check_evidence(command, dict(returncode=0, output='Tests: 3 passed, 3 total'))['status'], 'tests_passed')
        self.assertEqual(check_evidence(command, dict(returncode=0, output='No tests found, exiting with code 0'))['status'], 'no_tests')

    def test_public_requirements_are_included_and_private_fields_are_not(self):
        r=dict(problem_statement='ISSUE', requirements='"PUBLIC_REQUIREMENTS"', interface='PUBLIC_INTERFACE',
               dockerhub_tag='pro', patch='GOLD_SECRET', test_patch='TEST_SECRET', fail_to_pass='CASE_SECRET',
               before_repo_set_cmd='HIDDEN_CHECKOUT')
        text=task_text(r)
        for key in ['ISSUE','PUBLIC_REQUIREMENTS','PUBLIC_INTERFACE']:self.assertIn(key,text)
        for key in ['GOLD_SECRET','TEST_SECRET','CASE_SECRET','HIDDEN_CHECKOUT']:self.assertNotIn(key,text)

    def test_images_workdir_and_shell_environment(self):
        r=dict(instance_id='a__b-1',dockerhub_tag='x'*160,repo='ansible/ansible',base_commit='0'*40)
        self.assertEqual(image_name(r),'jefzda/sweap-images:'+'x'*128)
        self.assertEqual(len(required_images(r)),1)
        runtime=MiniRuntime(r,dict(task_seconds=1200),'/tmp/test-pro')
        self.assertEqual(runtime.cwd,'/app')
        self.assertEqual(runtime.shell_env,['PYTHONPATH=/app/lib'])
        del r['dockerhub_tag'];r['repo']='django/django'
        self.assertEqual(len(required_images(r)),2)
        self.assertEqual(MiniRuntime(r,dict(task_seconds=1200),'/tmp/test-verified').cwd,'/testbed')

    def test_language_test_paths_are_not_exported_as_source(self):
        for p in ['x/y_test.go','x/y.test.ts','x/y.spec.tsx','tests/test_a.py']:
            self.assertTrue(is_test_path(p),p)
        for p in ['x/testing.go','x/testable.ts','package-lock.json']:
            self.assertFalse(is_test_path(p),p)

    def test_eval_checkout_does_not_reset_submitted_source(self):
        text=extract_test_checkout('git reset --hard BASE\ngit clean -fd\ngit checkout BASE\ngit checkout GOLD -- tests/test_a.py')
        self.assertEqual(text,'git checkout GOLD -- tests/test_a.py')

    def test_python_parser_preserves_spaces_inside_parameter_ids(self):
        log='tests/a.py::test_x[param with spaces] PASSED [ 50%]\n__PRO__FAILED\ttests/a.py::test_y\n'
        result=parse_log_pro_pytest(log,None)
        self.assertEqual(result['tests/a.py::test_x[param with spaces]'],'PASSED')
        self.assertEqual(result['tests/a.py::test_y'],'FAILED')

    def test_go_and_jest_parsers_do_not_mark_absent_tests_as_passed(self):
        self.assertEqual(parse_log_pro_gotest('    --- PASS: TestFoo/subcase (0.00s)',None),{'TestFoo/subcase':'PASSED'})
        result=parse_log_pro_jest('[PASSED] hooks/a.test.ts | outer inner test',None)
        self.assertEqual(result['hooks/a.test.ts | inner test'],'PASSED')
        self.assertNotIn('missing',result)

    def test_eval_language_commands_preserve_installed_toolchains(self):
        row=dict(repo='ansible/ansible',repo_language='python',fail_to_pass=['test/a.py::test_x'],pass_to_pass=[],before_repo_set_cmd='')
        self.assertIn('PYTHONPATH=/app/lib',make_eval_script(row))
        row.update(repo='flipt-io/flipt',repo_language='go',selected_test_files_to_run=['internal/server/a_test.go'],fail_to_pass=['TestFoo'])
        self.assertIn('./internal/server',make_eval_script(row))


if __name__=='__main__':unittest.main()
