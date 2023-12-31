import os
import pathlib
from subprocess import call
from typing import List

import pandas as pd
from pydriller import Git
from tqdm import tqdm

from miner_py_src.call_graph import CFG, generate_cfg
from miner_py_src.exceptions import FunctionDefNotFoundException
from miner_py_src.miner_py_utils import get_function_defs
from miner_py_src.stats import FileStats
from miner_py_src.tree_sitter_lang import parser as tree_sitter_parser
from utils import create_logger

logger = create_logger("exception_miner", "exception_miner.log")


def fetch_gh(projects, dir='projects/py/'):
    for index, row in projects.iterrows():
        project = row['name']
        try:
            path = os.path.join(os.getcwd(), dir, project)
            git_cmd = "git clone {}.git --recursive {}".format(
                row['repo'], path)
            call(git_cmd, shell=True)
            logger.warning("EH MINING: cloned project")
        except Exception as e:
            logger.warning(f"EH MINING: error cloing project {project} {e}")


def fetch_repositories(project)->list[str]:

    if not os.path.exists("output/pytlint"):
        os.mkdir("output/pytlint")

    if not os.path.exists(f"output/pytlint/{project}"):
        os.mkdir(f"output/pytlint/{project}")

    try:
        path = os.path.join(os.getcwd(), "projects/py", str(project))
        git_cmd = "git clone {}.git --recursive {}".format(row["repo"], path)
        call(git_cmd, shell=True)
        logger.warning(
            "Exception Miner: Before init git repo: {}".format(project))
        gr = Git(path)
        logger.warning(
            "Exception Miner: After init git repo: {}".format(project))

        files = [
            f
            for f in gr.files()
            if pathlib.Path(rf"{f}").suffix == ".py" and not os.path.islink(f)
        ]

        return files

    except Exception as ex:
        logger.warning(
            "Exception Miner: error in project: {}, error: {}".format(
                project, str(ex))
        )
        return []


def __get_method_name(node):  
    for child in node.children:
        if child.type == 'identifier':
            return child.text.decode("utf-8")


def collect_parser(files, project_name):

    df = pd.DataFrame(
        columns=["file", "function", "func_body", "str_uncaught_exceptions", "n_try_except", "n_try_pass", "n_finally",
                 "n_generic_except", "n_raise", "n_captures_broad_raise", "n_captures_try_except_raise", "n_captures_misplaced_bare_raise",
                 "n_try_else", "n_try_return", "str_except_identifiers", "str_raise_identifiers", "str_except_block", "n_nested_try", 
                 "n_bare_except", "n_bare_raise_finally"]
    )

    file_stats = FileStats()
    pbar = tqdm(files)
    func_defs: List[str] = []  
    for file_path in pbar:
        pbar.set_description(f"Processing {str(file_path)[-40:].ljust(40)}")

        with open(file_path, "rb") as file:
            try:
                content = file.read()
            except UnicodeDecodeError as ex:
                tqdm.write(
                    f"###### UnicodeDecodeError Error!!! file: {file_path}.\n{str(ex)}"
                )
                continue
        try:
            tree = tree_sitter_parser.parse(content)
        except SyntaxError as ex:
            tqdm.write(
                f"###### SyntaxError Error!!! file: {file_path}.\n{str(ex)}")
        else:
            captures = get_function_defs(tree)
            for child in captures:
                # print("Function: ", __get_method_name(child))
                function_identifier = __get_method_name(child)
                if function_identifier is None:
                    raise FunctionDefNotFoundException(
                        f'Function identifier not found:\n {child.text}')

                func_defs.append(function_identifier)
                file_stats.metrics(child, file_path)
                metrics = file_stats.get_metrics(child)
                df = pd.concat(
                    [
                        pd.DataFrame(
                            [{
                                "file": file_path,
                                "function": __get_method_name(child),
                                "func_body": child.text.decode("utf-8"),
                                'str_uncaught_exceptions': '',
                                **metrics                                
                            }],
                            columns=df.columns,
                        ),
                        df,
                    ],
                    ignore_index=True,
                )
    file_stats.num_files += len(files)
    file_stats.num_functions += len(func_defs)

    logger.warning(f"before call graph...")

    call_graph = generate_cfg(str(project_name), os.path.normpath(
        f"projects/py/{str(project_name)}"))

    if call_graph is None:
        call_graph = {}

    catch_nodes = {}
    raise_nodes = {}
    for func_name in call_graph.keys():
        if not func_name.startswith('...'):
            continue  # skip external libraries

        names = func_name[3:].split('.')
        if len(names) == 1:
            continue  # skip built-in functions

        module_path = '/'.join(names[0:-1])
        func_identifier = names[-1]

        query = df[(df['file'].str.contains(module_path) &
                    df['function'].str.fullmatch(func_identifier))]

        if query.empty:
            continue

        if query.iloc[0]['str_raise_identifiers']:
            raise_nodes[func_name] = query.iloc[0]['str_raise_identifiers'].split(
                ' ')
        if query.iloc[0]['str_except_identifiers']:
            catch_nodes[func_name] = query.iloc[0]['str_except_identifiers'].split(
                ' ')

    call_graph_cfg = CFG(call_graph, catch_nodes)
    logger.warning(f"before parse the nodes from call graph...")

    for func_name, raise_types in raise_nodes.items():
        # func_file_raise, func_identifier_raise = func_name_raise.split(':')
        cfg_uncaught_exceptions = call_graph_cfg.get_uncaught_exceptions(
            func_name, raise_types)
        if cfg_uncaught_exceptions == {}:
            continue

        for f_full_identifier, uncaught_exceptions in cfg_uncaught_exceptions.items():
            module_path, func_identifier = ('', '')
            names = f_full_identifier.split('.')
            if len(names) == 1:
                func_identifier = names[0]
            else:
                module_path = names[0]
                func_identifier = names[-1]

            query = df[(df['file'].str.contains(module_path) &
                        df['function'].str.fullmatch(func_identifier))]

            if query.empty:
                continue

            idx = int(query.iloc[0].name)  # type: ignore

            for uncaught_exception in uncaught_exceptions:
                old_value = str(
                    df.iloc[idx, df.columns.get_loc('str_uncaught_exceptions')])

                # append uncaught exception
                df.iloc[idx, df.columns.get_loc(
                    'str_uncaught_exceptions')] = (old_value + f' {func_name}:{uncaught_exception}').strip()

    os.makedirs("output/parser/", exist_ok=True)
    logger.warning(f"Before write to csv: {df.shape}")
    df.to_csv(f"output/parser/{project_name}_stats.csv", index=False)


if __name__ == "__main__":
    projects = pd.read_csv("projects_py.csv", sep=",")
    for index, row in projects.iterrows():
        files = fetch_repositories(row['name'])
        if len(files) > 0:
            collect_parser(files, row['name'])
        else:
            continue
