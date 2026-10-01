import os
import subprocess
import pandas as pd
import hashlib
import csv
import re
import traceback

from ptm_usage_callerByNumber import fetch
from ptm_files_called_imports import find_called_imports

# Use PROJECT_DIR environment variable or fallback
project_directory = os.getenv("PROJECT_DIR", "/path/to/your/project")

unique_references = set()
unique_files = {}
paths_def = []
associated_repo = ""

def collect_method_references(data, csv_rows, depth, caller_name=""):
    for entry in data:
        class_name = entry.get("Class Name", "")
        method_name = entry.get("Method Name", "")

        for file, lines in entry.get("Definitions", {}).items():
            for line in lines:
                csv_rows.append(["Definition", depth, caller_name, file, line])

        for file, urls in entry.get("Function References", {}).items():
            for url in urls:
                csv_rows.append(["Reference", depth, caller_name, file, url])

def find_function_references(project_directory, class_method_name):
    results = []
    inittrue = False

    if '.' in class_method_name:
        parts = class_method_name.rsplit('.', 1)
        class_name, method_name = parts
        if method_name == "__init__":
            method_name = class_name
            inittrue = True
    else:
        class_name = ""
        method_name = class_method_name

    if not method_name:
        return ""
    if not class_name and method_name == 'main':
        return ""

    definitions = ""
    function_references = ""
    try:
        if not class_name:
            function_references = git_grep_method_references(project_directory, method_name)
            if not len(function_references):
                function_references = git_grep_class_method_references(project_directory, class_name, method_name)
        else:
            if inittrue:
                function_references = git_grep_inittrue_method_references(project_directory, class_name, method_name)
            else:
                function_references = git_grep_class_method_references(project_directory, class_name, class_name + "." + method_name)
                if not len(function_references):
                    function_references = git_grep_class_method_references(project_directory, class_name, method_name)
            definitions = git_grep_class_definitions(project_directory, class_name)
    except Exception as e:
        print("exception grep: " + str(e))

    result = {
        'Class Name': class_name,
        'Method Name': method_name,
        'Definitions': definitions,
        'Function References': function_references
    }
    results.append(result)
    return results

def git_grep_class_method_definitions(project_directory, class_name, method_name):
    command = (
        f"git -C {project_directory} grep -nE 'class {class_name}\\s*\\(|def {method_name}\\s*\\(' -- '*.py'"
        f"| grep -v '\\s*#'"
    )
    return run_git_grep(command, project_directory)

def git_grep_class_method_references(project_directory, class_name, method_name):
    command = (
        f"git -C {project_directory} grep -nE '\\b\\w+\\.{method_name}\\s*' -- '*.py'"
        f"| grep -vE '\\bdef {method_name}\\s*\\(' "
        f"| grep -v '\\bclass\\b' "
        f"| grep -v '\\s*#'"
    )
    return run_git_grep(command, project_directory)

def git_grep_method_references(project_directory, method_name):
    command = (
        f"git -C {project_directory} grep -nE '(^|[^.\\w]){method_name}\\s*\\(' -- '*.py'"
        f"| grep -vE '\\bdef {method_name}\\s*\\(' "
        f"| grep -v '\\bclass\\b' "
        f"| grep -v '\\s*#'"
    )
    return run_git_grep(command, project_directory)

def git_grep_inittrue_method_references(project_directory, class_name, method_name):
    command = (
        f"git -C {project_directory} grep -nE '(\\.{method_name}|\\b{method_name})\\s*\\(' -- '*.py'"
        f"| grep -vE '\\bdef {method_name}\\s*\\(' "
        f"| grep -v '\\bclass\\b' "
        f"| grep -v '\\s*#'"
    )
    return run_git_grep(command, project_directory)

def git_grep_class_definitions(project_directory, class_name):
    command = f"git -C {project_directory} grep -nE 'class {class_name}\\s*' -- '*.py'"
    return run_git_grep(command, project_directory)

def run_git_grep(command, project_directory):
    result = {}
    try:
        output = subprocess.check_output(command, shell=True, text=True)
        for line in output.splitlines():
            file_path, line_number, _ = line.split(':', 2)
            file_path_abs = os.path.abspath(os.path.join(project_directory, file_path))
            if file_path_abs not in result:
                result[file_path_abs] = []
            result[file_path_abs].append(int(line_number))
    except subprocess.CalledProcessError:
        pass
    return result

def print_method_references(data, output_lines):
    for entry in data:
        output_lines.append(f"Class Name: {entry['Class Name']}")
        output_lines.append(f"Method Name: {entry['Method Name']}")
        output_lines.append("\nDefinitions:")
        for file, lines in entry['Definitions'].items():
            for line in lines:
                output_lines.append(f"    {file}:{line}")
        output_lines.append("\nFunction References:")
        for file, urls in entry['Function References'].items():
            for url in urls:
                output_lines.append(f"    {file}:{url}")
        output_lines.append("\n" + "=" * 40 + "\n")

def find_github_repo_by_name(root_directory, repo_name, owner_reponame):
    for dirpath, dirnames, _ in os.walk(root_directory):
        for dirname in dirnames:
            if dirname == owner_reponame:
                owner_path = os.path.join(dirpath, dirname)
                repo_path = os.path.join(owner_path, repo_name)
                if owner_reponame in repo_path:
                    return repo_path, dirpath
    return "", ""

def get_project_path(relative_path=""):
    return os.path.join(project_directory, relative_path)

def find():
    csv_rows = [["Type", "Depth", "Caller Name", "Caller File", "Class Name", "Method Name", "File", "Line/URL"]]
    global associated_repo

    df_c = pd.read_csv(get_project_path("input_csvs/401_caller.csv"))
    df_gh1 = pd.read_csv(get_project_path("input_csvs/hf_github_url.csv"))

    df_c['github_url'] = df_c['github_url'].str.lower()
    df_gh1['github_url'] = df_gh1['github_url'].str.lower()

    df_filtered = df_c[df_c['github_url'].isin(df_gh1['github_url'])]
    repos = sorted(list(set(df_filtered['github_url'].to_list())))

    todos = [
        'github.com/facebookresearch/fairseq',
        'github.com/arise-initiative/robomimic',
        'github.com/cuongnn218/zalo_ltr_2021',
        'github.com/intel-analytics/bigdl',
        'github.com/microsoft/adamix',
        'github.com/salesforce/coderl',
        'github.com/microsoft/lmops',
        'github.com/alibabaresearch/damo-convai',
        'github.com/facebookresearch/parlai',
        'github.com/flairnlp/flair',
        'github.com/huawei-noah/noah-research',
        'github.com/mlflow/mlflow',
        'github.com/pfnet-research/distilled-feature-fields'
    ]

    for repo in repos:
        if repo.lower() in todos:
            continue

        associated_repo = repo
        repo_save = repo.replace("github.com/", "").replace("/", "_")
        df = df_filtered[df_filtered['github_url'] == repo]
        paths = df['file_path']
        repo_owner, repo_name = repo.split("/")[-2:]
        repo_path = get_project_path(f"{repo_owner}_{repo_name}/{repo_name}")

        if not repo_path:
            continue

        next_caller_file_path = set()
        repo_dir = os.path.join("output_csvs", repo_save)
        os.makedirs(repo_dir, exist_ok=True)

        for path in paths:
            paths_def.append(get_project_path(path))
            df_p = df[df['file_path'] == path]
            callers = df_p['caller'].to_list()
            level_data = []

            try:
                for class_method_name in set(callers):
                    if not class_method_name or isinstance(class_method_name, float):
                        continue

                    output_lines = []
                    csv_rows = []
                    depth_info = []

                    full_file_path = get_project_path(path)
                    unique_references.add((full_file_path, class_method_name))
                    results = find_function_references(repo_path, class_method_name)
                    depth = 1
                    collect_method_references(results, csv_rows, depth, class_method_name)

                    next_caller_file_path, references = check_results(results, project_directory, path, next_caller_file_path)
                    for file_path, caller in references:
                        level_data.append({"caller": caller, "file_path": file_path})
                    depth_info.append({"depth": 1, "data": level_data})

                    while depth <= 5 and next_caller_file_path:
                        new_caller_file_path = set()
                        depth += 1
                        for file_path, caller in next_caller_file_path:
                            results = find_function_references(repo_path, caller)
                            collect_method_references(results, csv_rows, depth, caller)
                            level_data = []
                            new_caller_file_path, references = check_results(results, project_directory, path, new_caller_file_path)
                            for file_path, caller in references:
                                level_data.append({"caller": caller, "file_path": file_path})
                            depth_info.append({"depth": depth, "data": level_data})
                        next_caller_file_path = new_caller_file_path

                    def sanitize_filename(s):
                        return re.sub(r'[^\w\-_.]', '_', s)

                    path_base = sanitize_filename(os.path.basename(path))
                    caller_name = sanitize_filename(class_method_name.split('.')[-1])
                    path_hash = hashlib.md5(path.encode()).hexdigest()[:8]
                    csv_filename = f"{path_base}__{caller_name}__{path_hash}.csv"
                    csv_path = os.path.join(repo_dir, csv_filename)

                    with open(csv_path, mode="w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(["def/reference", "depth", "caller", "file_path", "line"])
                        for row in csv_rows:
                            writer.writerow(row)
            except Exception:
                traceback.print_exc()

def check_results(results, dir, path, next_caller_file_path):
    global unique_files
    global unique_references
    next_level_reference = set()

    for result in results:
        function_references = result['Function References']
        for file_path, lines in function_references.items():
            for line in lines:
                caller = fetch(file_path, line)
                if (file_path, caller) not in unique_references:
                    unique_references.add((file_path, caller))
                    next_level_reference.add((file_path, caller))
                    if caller:
                        next_caller_file_path.add((file_path, caller))
                if file_path not in unique_files:
                    unique_files[file_path] = set()
                unique_files[file_path].add(caller)

    return next_caller_file_path, next_level_reference

if __name__ == "__main__":
    find()
