#!/usr/bin/python3
# -*- coding: utf-8 -*-

# <legal>
# 'Redemption' Automated Code Repair Tool
#
# Copyright 2023, 2024 Carnegie Mellon University.
#
# NO WARRANTY. THIS CARNEGIE MELLON UNIVERSITY AND SOFTWARE ENGINEERING
# INSTITUTE MATERIAL IS FURNISHED ON AN 'AS-IS' BASIS. CARNEGIE MELLON
# UNIVERSITY MAKES NO WARRANTIES OF ANY KIND, EITHER EXPRESSED OR IMPLIED,
# AS TO ANY MATTER INCLUDING, BUT NOT LIMITED TO, WARRANTY OF FITNESS FOR
# PURPOSE OR MERCHANTABILITY, EXCLUSIVITY, OR RESULTS OBTAINED FROM USE OF
# THE MATERIAL. CARNEGIE MELLON UNIVERSITY DOES NOT MAKE ANY WARRANTY OF ANY
# KIND WITH RESPECT TO FREEDOM FROM PATENT, TRADEMARK, OR COPYRIGHT
# INFRINGEMENT.
#
# Licensed under a MIT (SEI)-style license, please see License.txt or
# contact permission@sei.cmu.edu for full terms.
#
# [DISTRIBUTION STATEMENT A] This material has been approved for public
# release and unlimited distribution.  Please see Copyright notice for
# non-US Government use and distribution.
#
# This Software includes and/or makes use of Third-Party Software each
# subject to its own license.
#
# DM23-2165
# </legal>

import os
import sys
import glob
import pdb
import subprocess
import yaml
import argparse
import logging
import shutil
test_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(1, test_dir + "/..")
import test_known_inputs_output
import end_to_end_acr
import json
from enum import Enum
from util import read_json_file

stop = pdb.set_trace

# create an instance of the logger
logger = logging.getLogger('test_runner_logger')

logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
file_handler = logging.FileHandler('logs.log')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
dict_alert_info_patches_fps = {}

class PassStatus(Enum):
    NOT_RUN = 0
    FAILED = 1    # it was run but failed
    PASSED = 2    # it was run and passed
    NO_ANS = 3    # no ".ans" file was found; counts as a fail
    NO_PATCH = 5  # the interdiff for the .ans file matched BUT no patch and the is_false_positive field != True

def parse_args():
    parser = argparse.ArgumentParser(description='Runs tests of code that creates repaired source code files')
    parser.add_argument("tests_file", type=str, help="The YAML file specifying a test list with inputs and answers")
    parser.add_argument("-k", type=str, dest="stringinput", help="only run tests which match the given substring expression")
    parser.add_argument("--check-ans", action="store_true", dest="check_ans")
    parser.add_argument("--create-ans", action="store_true", dest="create_ans", help="Create '.ans' file if it doesn't exist")
    parser.add_argument("-e", action="store_true", dest="examine_is_fp", help="To determine if the test fails, examine the is_false_positive and patch attributes. Writes output to file named <YAML_FILENAME>.alerts_info.json")
    
    argparse.cmdline_args = parser.parse_args()
    if argparse.cmdline_args.create_ans:
        argparse.cmdline_args.check_ans = True
    return argparse.cmdline_args

def read_yaml_file(filename):
    with open(filename, 'r') as f:
        data = yaml.load(f, Loader=yaml.FullLoader)
    return data

class DummyException(Exception):
    pass

def run_test(**kwargs):
    # To print out test info as tests are being run, uncomment the following line:
    print("Running test: %r" % kwargs)
    current_dir = os.getcwd()
    no_try = (os.getenv('pytest_no_catch') == "true")
    if no_try:
        # This will catch all exceptions
        ExceptionClass = DummyException
    else:
        # This won't catch any exceptions; it's for breaking into the debugger
        # when an error occurs.
        ExceptionClass = BaseException
    try:
        end_to_end_acr.run(**kwargs)
    except ExceptionClass as exc:
        os.chdir(current_dir)
        # Exception info printed to file
        print("!!! Error: " + str(exc))
        logger.error("Exception while running test: %r" % kwargs)
        logger.exception(exc)


################################################################################
################################################################################


# If stringinput is empty string or doesn't match any test name in the .yml file, test passes.
def run(stringinput, tests_file,
        directory=".",
        step_dir="/host/code/acr/test/step",
        repair_in_place=None,
        repair_includes_mode=None,
        **extra_kwargs):
    tests_info = read_yaml_file(os.path.join(directory, tests_file))
    out_location = os.path.realpath("out/")
    all_passed_or_none_tested = 1
    count_results_compared = 0

    if not os.path.exists(out_location):
        os.makedirs(out_location)
    step_dir_prev_existed = os.path.isdir(step_dir)
    if(step_dir_prev_existed == False):
        print("step_dir didn't exist")
        cmd = "mkdir -p {0}".format(step_dir)
        os.system(cmd)

    all_diff_results = []

    for test in tests_info['tests']:
        if test.get('name') is None:
            test['name'] = os.path.splitext(test['input']['cfile'])[0]
        do_this = (not stringinput) or (stringinput in test['name'])
        cur_answer_file = os.path.realpath(os.path.join(directory, test['answer_file']))
        if do_this:
            count_results_compared += 1
            if ("base_dir" in test['input']):
                base_dir = os.path.realpath(test['input']['base_dir'])
                cur_c_file = os.path.realpath(os.path.join(base_dir, test['input']['cfile']))
            else:
                base_dir = None
                cur_c_file = os.path.realpath(test['input']['cfile'])

            if "hfile" in test['input'].keys():
                file_to_repair = test['input']['hfile']
                if ("base_dir" in test['input']):
                    file_to_repair = os.path.realpath(os.path.join(base_dir, file_to_repair))
                else:
                    file_to_repair = os.path.realpath(os.path.join(file_to_repair))
                repair_includes_mode = True
            else:
                file_to_repair = cur_c_file

            if base_dir and file_to_repair.startswith(base_dir+"/"):
                filename = file_to_repair[len(base_dir+"/"):]
            else:
                filename = os.path.basename(file_to_repair)
            file_prefix = os.path.splitext(os.path.basename(filename))[0]

            if (test['input'].get('compile_cmds_file', "autogen") != "autogen"):
                cur_compile_cmds_file = os.path.realpath(test['input']['compile_cmds_file'])
            else:
                cur_compile_cmds_file = "autogen"
            cur_alerts_file = os.path.realpath(os.path.join(directory, test['input']['alerts_file']))

            # Delete already-existing files, to avoid falsely reporting that
            # the test succeeds when it really fails.
            old_files = (
                glob.glob("%s/%s.*" % (out_location, file_prefix)) +
                glob.glob("%s/%s.*" % (step_dir, file_prefix)))
            for old_file in old_files:
                os.remove(old_file)

            # TODO: check each of files exists
            run_test(source_file=cur_c_file,
                     compile_commands=cur_compile_cmds_file,
                     alerts=cur_alerts_file,
                     out_src_dir=out_location,
                     base_dir=base_dir,
                     step_dir=step_dir,
                     repair_in_place=repair_in_place,
                     repair_includes_mode=repair_includes_mode)
            test_results_file = os.path.join(out_location, filename)
            if cur_answer_file.endswith(".json"):
                test_results_file = os.path.join(step_dir, os.path.basename(cur_answer_file))

            def print_test_cmd():
                print("        Test command: " + __file__ + " " + tests_file + " -k " + test['name'])

            # If no repair was possible, ACR produces no output file.
            print("%s:" % test['name'])
            all_diff_results = []
            if (os.path.exists(test_results_file)):
                if (os.path.exists(cur_answer_file)):
                    diff_results = subprocess.run(['diff', test_results_file, cur_answer_file], capture_output=True)
                    if diff_results.returncode != 0:
                        print("  FAIL: test result doesn't match answer key.\n")
                        all_diff_results.append([test['name'], diff_results.args, diff_results.stdout])
                        all_passed_or_none_tested = 0
                        print_test_cmd()
                    else:
                        print("  pass: test result and answer key are same")
                        cleanup(test_results_filepath=test_results_file, out_location=out_location, filename=filename, step_dir=step_dir, repair_in_place=repair_in_place, repair_includes_mode=repair_includes_mode, file_prefix=file_prefix)
                else:
                    print("  FAIL: test result exists, but answer key does not exist")
                    print("        missing answer key : " + cur_answer_file)
                    all_passed_or_none_tested = 0
                    print_test_cmd()
            else:
                if os.path.exists(cur_answer_file):
                    print("  FAIL: test result does not exist, but answer key does exist")
                    print("        missing test result file: " + test_results_file)
                    all_passed_or_none_tested = 0
                    print_test_cmd()
                else:
                    print("  pass: test result and answer key both do not exist")
                    cleanup(test_results_filepath=test_results_file, out_location=out_location, filename=filename, step_dir=step_dir, repair_in_place=repair_in_place, repair_includes_mode=repair_includes_mode, file_prefix=file_prefix)

    if all_diff_results:
        print("#"*70)
        print("Diff results:")
        print("#"*70)
        for (test_name, diff_args, diff_stdout) in all_diff_results:
            print("Test: " + test_name)
            print(diff_args)
            print(diff_stdout.decode())
            print("#"*70)

    dir_final_cleanup(step_dir, step_dir_prev_existed)
    print("count_results_compared is ", count_results_compared)
    return all_passed_or_none_tested

################################################################################
################################################################################

# `run_and_check_if_answer` checks if the answer file exists, but continues testing if `stop_if_no_answer_file` is False.
# It outputs a count of tests and number passed.
# Currently, if `stringinput` is empty string or doesn't match any test name in the .yml file, test passes.
def run_and_check_if_answer(examine_is_fp, stringinput, tests_file,
                            directory=".",
                            step_dir="/host/code/acr/test/step",
                            repair_in_place=None,
                            repair_includes_mode=None,
                            stop_if_no_answer_file=False,
                            **extra_kwargs):
    print("run_and_check_if_answer for tests_file: ", tests_file)

    tests_info = read_yaml_file(directory + "/" + tests_file)
    out_location = os.path.realpath("out/")
    all_passed_or_none_tested = 1
    count_results_compared = 0
    count_skipped_tests = 0
    pass_status = PassStatus.NOT_RUN 
    answer_file_exists = False 
    is_fp = False
    a_patch = False
    dict_alert_info_patches_fps.clear()

    if not os.path.exists(out_location):
        os.makedirs(out_location)
    step_dir_prev_existed = os.path.isdir(step_dir)
    if(step_dir_prev_existed == False):
        cmd = "mkdir -p {0}".format(step_dir)
        os.system(cmd)

    all_diff_results = []

    for test in tests_info['tests']:
        if test.get('name') is None:
            test['name'] = os.path.splitext(test['input']['cfile'])[0]
        do_this = (not stringinput) or (stringinput in test['name'])
        cur_answer_file = os.path.realpath(os.path.join(directory, test['answer_file']))
        answer_file_exists = os.path.exists(cur_answer_file)
        if(do_this and (answer_file_exists or (stop_if_no_answer_file == False))):
            count_results_compared += 1
            if ("base_dir" in test['input']):
                base_dir = os.path.realpath(test['input']['base_dir'])
                cur_c_file = os.path.realpath(os.path.join(base_dir, test['input']['cfile']))
            else:
                base_dir = None
                cur_c_file = os.path.realpath(test['input']['cfile'])

            if "hfile" in test['input'].keys():
                file_to_repair = test['input']['hfile']
                if ("base_dir" in test['input']):
                    file_to_repair = os.path.realpath(os.path.join(base_dir, file_to_repair))
                else:
                    file_to_repair = os.path.realpath(os.path.join(file_to_repair))
                repair_includes_mode = True
            else:
                file_to_repair = cur_c_file

            if base_dir and file_to_repair.startswith(base_dir+"/"):
                filename = file_to_repair[len(base_dir+"/"):]
            else:
                filename = os.path.basename(file_to_repair)
            file_prefix = os.path.splitext(os.path.basename(filename))[0]

            if (test['input'].get('compile_cmds_file', "autogen") != "autogen"):
                cur_compile_cmds_file = os.path.realpath(test['input']['compile_cmds_file'])
            else:
                cur_compile_cmds_file = "autogen"
            cur_alerts_file = os.path.realpath(os.path.join(directory, test['input']['alerts_file']))

            # Delete already-existing files, to avoid falsely reporting that
            # the test succeeds when it really fails.
            old_files = (
                glob.glob("%s/%s.*" % (out_location, file_prefix)) +
                glob.glob("%s/%s.*" % (step_dir, file_prefix)))
            for old_file in old_files:
                os.remove(old_file)

            # TODO: check each of files exists
            test_name = test['name']
            print("Test command: " + __file__ + " " + tests_file + " --check-ans -k " + test_name)
            run_test(source_file=cur_c_file,
                     compile_commands=cur_compile_cmds_file,
                     alerts=cur_alerts_file,
                     out_src_dir=out_location,
                     base_dir=base_dir,
                     step_dir=step_dir,
                     repair_in_place=repair_in_place,
                     repair_includes_mode=repair_includes_mode)
            test_results_file = os.path.join(out_location, filename)
            if cur_answer_file.endswith(".json"):
                test_results_file = os.path.join(step_dir, os.path.basename(cur_answer_file))

            file_prefix = os.path.splitext(os.path.basename(cur_c_file))[0] # use for intermediate filenames and cleanup
            # If alert for a .h file, then to match brain output name, use c file

            pass_status = determine_pass_status(examine_is_fp=examine_is_fp,
                          step_dir=step_dir,
                          repair_in_place=repair_in_place,
                          repair_includes_mode=repair_includes_mode,
                          stop_if_no_answer_file=stop_if_no_answer_file,
                          file_prefix=file_prefix,
                          file_to_repair=file_to_repair,
                          test_name=test_name,
                          test_results_file=test_results_file,
                          answer_file_exists=answer_file_exists,
                          filename=filename,
                          cur_answer_file=cur_answer_file,
                          out_location=out_location,
                          extra_kwargs=extra_kwargs)
        else:
            count_skipped_tests += 1
            pass_status = PassStatus.NOT_RUN
            if(answer_file_exists == False): 
                print("  pass(not run): no answer file, and this test wasn't run since stop_if_no_answer_file is True.")
            else:
                print("  pass(not run): this test wasn't run, no test string match though it had an answer file.")                
            pass
        if((pass_status != PassStatus.PASSED) and (pass_status != PassStatus.NOT_RUN)):
            all_passed_or_none_tested = 0
            print("set all_passed_or_none_tested = 0 and pass_status: ", pass_status)

    if(examine_is_fp):
        # Select info goes to *alerts_info.json file, per test.yml file.
        # Convert and write JSON object to file        
        alerts_info_filename = tests_file+".alerts_info.json"
        with open(alerts_info_filename, "w") as outfile: 
            json.dump(dict_alert_info_patches_fps, outfile, sort_keys=True, indent=4)
        outfile.close()
    
    # reset the dictionary for the next time test_runner.py is called
    dict_alert_info_patches_fps.clear()
    
    if all_diff_results:
        print("#"*70)
        print("Diff results:")
        print("#"*70)
        for (test_name, diff_args, diff_stdout) in all_diff_results:
            print("Test: " + test_name)
            print(diff_args)
            print(diff_stdout.decode())
            print("#"*70)

    dir_final_cleanup(step_dir, step_dir_prev_existed)
    print("count_results_compared is ", count_results_compared, " and count_skipped_tests is ", count_skipped_tests)
    return all_passed_or_none_tested


def dir_final_cleanup(step_dir, step_dir_prev_existed):
    if os.getenv('pytest_keep') == "true":
        return
    if(step_dir_prev_existed == False):
        cmd = "rm -r {0}".format(step_dir)
        os.system(cmd)

# this function can clean up after passing tests
def cleanup(filename, out_location, step_dir, test_results_filepath=None, file_prefix=None, repair_in_place=None, repair_includes_mode=None, base_dir=None, additional_files=None):
    if os.getenv('pytest_keep') == "true":
        return
    if(additional_files != None):
        results_filepath = os.path.realpath(os.path.join(additional_files))
        print("test_runner cleanup: results_filepath is ", results_filepath)
        cmd = "rm {0}".format(results_filepath)
        os.system(cmd)
    else:
        if(repair_in_place == True):
            test_results_filepath = filename
        if(file_prefix == None):
            file_prefix = os.path.splitext(os.path.basename(filename))[0]
        # Remove each separately so missing result files could be caught
        if(test_results_filepath == None):
            test_results_filepath = out_location + "/" + filename
        cmd = "rm {0}".format(test_results_filepath)
        os.system(cmd)
        for x in ("brain", "ear"):
            results_filepath = os.path.realpath(os.path.join(step_dir,file_prefix+"."+x+"-out.json"))
            cmd = "rm {0}".format(results_filepath)
            os.system(cmd)
        dot_h_results_filepath = out_location+"/"+"acr.h"
        cmd3 = "rm {0}".format(dot_h_results_filepath)
        os.system(cmd3)
        # delete .ll results
        ll_results_filepath = os.path.realpath(os.path.join(step_dir,file_prefix+".ll"))
        cmd4 = "rm {0}".format(ll_results_filepath)
        os.system(cmd4)
        nulldom_results_filepath = os.path.realpath(os.path.join(step_dir,file_prefix+".nulldom.json"))
        cmd5 = "rm {0}".format(nulldom_results_filepath)
        os.system(cmd5)

# This function gathers per-alert data from a brain output file and then edits a dictionary as/if needed.
def get_alert_patch_and_fp_info_from_brain_output(file_prefix, step_dir, file_to_repair):

    is_fp = False
    patch_found = False
    alert_id = 0
    return_is_fp = False
    return_patch = False

    brain_out_file = os.path.realpath(os.path.join(step_dir, file_prefix+".brain-out.json"))
    # Below verifies processes `is_false_positive` field, using a brain output file with is_no path and is_fp true
    # brain_out_file = os.path.realpath(os.path.join("/host/code/acr/test/already_repaired_null_01.brain-out.json"))
    if(brain_out_file):
        braindata = read_json_file(brain_out_file)
        # Iterate through the json list
        for x in braindata:
            alert_id = x["alert_id"]
            rule = x["rule"]
            file = x["file"]
            patch = ("patch" in x and x["patch"] != [])
            is_fp = ("is_false_positive" in x and ((x["is_false_positive"] == True) or (x["is_false_positive"] == "true")))

            if(file == file_to_repair):  
                return_is_fp = is_fp
                return_patch = patch
            # either enter new info OR
            # check if should modify 1. patch info (only False to True change); AND check if should modify
            #                        2. is_false_positive (only False to True change)
            try:
                assert(dict_alert_info_patches_fps[file][rule][alert_id])
                thispatch, this_is_fp = dict_alert_info_patches_fps[file][rule][alert_id]
                if((not thispatch) and patch_found): #only change if was False and this is True
                    dict_alert_info_patches_fps[file][rule][alert_id][0] = patch_found
                if((not this_is_fp) and is_fp): #only change if was False and this is True
                    dict_alert_info_patches_fps[file][rule][alert_id][1] = is_fp
            except:
                # Ensure that each nested key is there, prior to entering final list
                dict_alert_info_patches_fps.setdefault(file, {}).setdefault(rule, {})[alert_id] = [patch, is_fp]

    return(return_patch, return_is_fp)

def determine_pass_status(examine_is_fp,
                            step_dir,
                            repair_in_place,
                            repair_includes_mode,
                            stop_if_no_answer_file,
                            file_prefix,
                            file_to_repair,
                            test_name,
                            test_results_file,
                            answer_file_exists,
                            filename,
                            cur_answer_file,
                            out_location,
                           **extra_kwargs):

    ret_pass_status = PassStatus.NOT_RUN

    if(examine_is_fp == True):
        # For set of brain output files, log info per alert with no patch in any brain output file.
        # A brain output file may have info for more than one alert. Also, it is deleted after the test, by default.

        a_patch, is_fp = get_alert_patch_and_fp_info_from_brain_output(file_prefix, step_dir, file_to_repair) 
        print("get_alert_patch_and_fp_info_from_brain_output returned is_fp: ", is_fp)
        print("and a_patch: ", a_patch)

    # If no repair was possible, ACR produces no output file.
    print("%s:" % test_name)
    all_diff_results = []
    if (os.path.exists(test_results_file)):
        if (answer_file_exists or (stop_if_no_answer_file == False) or extra_kwargs.get("create_ans")):
            # The OSS repaired files are often large, so answer files are patch files (diff -u).
            # Therefore, our comparison is between an answer file (containing an older diff between 
            # repaired file and original file) and a diff between the newly-repaired file and the original file.
            # We use interdiff to ignore timestamp differences between diff files.
            diff_from_original = subprocess.run(['diff', '-u', test_results_file, file_to_repair], capture_output=True)
            cur_diff_file = step_dir + "/" + test_name + ".diff"
            with open(cur_diff_file, mode="w") as df:
                # Store diffs between the test_results_file and the original file
                df.write("%s" % diff_from_original.stdout.decode())
                # The decode() is so newlines are not converted to \n in file
            df.close()

            if extra_kwargs.get("create_ans") and not answer_file_exists:
                print("  Creating answer file: " + cur_answer_file)
                shutil.copy(cur_diff_file, cur_answer_file)

            diff_results = subprocess.run(['interdiff', cur_answer_file, cur_diff_file], capture_output=True)
            if os.getenv('pytest_keep') != "true":
                subprocess.run(['rm', cur_diff_file], capture_output=True)

            if((len(diff_results.stdout) != 0) or (len(diff_results.stderr) != 0)):
                print("  FAIL: test result doesn't match answer key. (run_and_check_if_answer)\n")
                all_diff_results.append([test_name, diff_results.args, diff_results.stdout])
                ret_pass_status = PassStatus.FAILED
            else:
                if((examine_is_fp == True) and (not a_patch) and (is_fp != True) and (answer_file_exists == True)):
                    print("  failed: examine_is_fp True, no patch, is_fp not True, answer file and interdiff test result matched")
                    ret_pass_status = PassStatus.NO_PATCH
                else:
                    print("  pass: test result and answer key are same")
                    ret_pass_status = PassStatus.PASSED
        else:
            print("  FAIL: test result exists, but answer key does not exist AND stop_if_no_answer_file is True")
            ret_pass_status = PassStatus.NO_ANS
    else:
        # ran test and no os.path.exists(test_results_file)
        if answer_file_exists:
            if(os.path.getsize(cur_answer_file) == 0):
                print("   pass: test result does not exist, and answer file is empty")
                ret_pass_status = PassStatus.PASSED
            else:
                print("  FAIL: test result does not exist, but non-null answer key does exist")
                print("        file: " + test_results_file)
                ret_pass_status = PassStatus.FAILED 
        else:
            print("  FAIL: test result and answer key both do not exist")
            ret_pass_status = PassStatus.NO_ANS # no .ans file, ran test because of special argument

    # Below cleans up ear, brain -out.json and some .ll files
    cleanup(test_results_filepath=test_results_file, out_location=out_location, filename=filename, step_dir=step_dir, repair_in_place=repair_in_place, repair_includes_mode=repair_includes_mode, file_prefix=file_prefix)

    return ret_pass_status

def test_two_good(stringinput, out_dir):
    assert run(stringinput, 'inputs.answers.yml') == 1
    # If assert returned 0, one or more tests ran then failed

def test_first_two_right_last_wrong(stringinput, out_dir):
    assert run(stringinput, "inputs.answers.lastfail.yml") == 0

def test_parameter_string_matches(stringinput, out_dir):
    assert run(stringinput, "in.out.substrings.match.some.names.yml") == 1

# def test_incorrect_assertion(stringinput, out_dir):
#     # With the incorrect assertion in the line below, pytest prints details of the failing last test and prints output from the first 2 passing tests.
#     assert run(stringinput, "inputs.answers.lastfail.yml") == 1

def test_errors(stringinput, out_dir):
    assert run(stringinput, "test_errors.yml") == 1

def test_array_null(stringinput, out_dir):
    assert run(stringinput, "array_null.yml") == 1


def main():
    cmdline_args = parse_args()
    if cmdline_args.check_ans:
        run_and_check_if_answer(**vars(cmdline_args))
    else:
        run(**vars(cmdline_args))


if __name__ == "__main__":
    main()
