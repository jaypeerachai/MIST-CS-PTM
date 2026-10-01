import ast
from bisect import bisect_right
from scalpel.SSA.const import SSA
from scalpel.core.mnode import MNode
import traceback


import logging

logging.basicConfig(filename='codeextraction_dep.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')

from lib2to3 import refactor

# Create an instance of RefactoringTool
refactor_tool = refactor.RefactoringTool(refactor.get_fixers_from_package('lib2to3.fixes'))


class ExtractSource():
    def __init__(self, source):
        self.mnode = MNode("local")
        self.mnode.source = source

        self.import_dict = {}
        self.func_defs = []
        self.func_calls = []
        self.importname = ""
        self.classname = ""
        self.method = ""
        self.function_with_startln = {}
        self.function_with_endln = {}
        self.all_options_import = []
        self.blob = ""
        self.params_info = {}
        self.targets_info = []

    def set_importname(self, importname):
        self.importname = importname

    def set_targets_info(self, targets):
        self.targets_info = targets

    def set_classname(self, classname):
        self.classname = classname

    def set_method(self, method):
        self.method = method

    def set_blob(self, blob):
        self.blob = blob

    def get_ast(self):
        try:
            self.mnode.gen_ast()
        except Exception as e:
            logging.error('exception in AST generation: ' + self.blob + str(e))

    def get_imports(self):
        try:
            self.import_dict = self.mnode.parse_import_stmts_extented()  # scalpel.core.mnode has been modified
            self._consider_import()
        except Exception as e:
            logging.error('exception in imports: ' + self.blob + str(e))

    def get_func_calls(self):
        try:
            self.func_calls = self.mnode.parse_func_calls()
        except Exception as e:
            print('exception in func_calls:' + self.blob  + str(e))

    def get_func_defs(self):
        try:
            self.func_defs = self.mnode.parse_func_defs_extended()    # scalpel.core.mnode has been modified
            self.extract_fun_defs()
        except Exception as e:
            logging.error('exception in func_defs: ' + self.blob + str(e))

    def _find_val(self, arr, x):
        i = bisect_right(arr, x)
        if i == 0:
            return arr[0]
        return arr[i - 1]

    def _consider_import(self):
        keys_list = list(self.import_dict.keys())
        for each_val in keys_list:
            dotted_keys = each_val.split('.')
            self.all_options_import.extend(dotted_keys)
        values_list = self.import_dict.values()
        for each_val in values_list:
            dotted_values = each_val.split('.')
            self.all_options_import.extend(dotted_values)
        self.all_options_import = list(set(self.all_options_import))

    def extract_fun_defs(self):
        for func_def in self.func_defs:
            func_name = func_def["name"]
            func_scope = func_def["scope"]
            lineno = func_def["lineno"]
            end_linno = func_def["end_linno"]
            if func_scope != 'mod':
                func_name = func_scope + "." + func_name

            self.function_with_startln[lineno] = {'name': func_name, 'scope': func_scope, 'end_linno': end_linno}
            self.function_with_endln[end_linno] = func_name

    # get the constant and variable
    def fetch_match_called(self):
        matched_result = []

        self.get_ast()
        # print(ast.dump(self.mnode.ast))
        if self.mnode.ast is None:
            logging.error('exception before 2to3 conversion: ' + self.blob)
            # source_code = self.mnode.source
            # try:
            #     python3_code = refactor_tool.refactor_string(source_code, "")
            #     self.mnode.source = str(python3_code)
            #     self.get_ast()
            # except Exception as e:
            #     # print(self.blob)
            #     logging.error('exception in 2to3 conversion: '+ self.blob + str(e))

        if self.mnode.ast is not None:
            self.get_imports()
            self.get_func_calls()
            # print(self.func_calls)
            try:
                for call_info in self.func_calls:
                    # print(call_info)
                    call_data = {}
                    call_name = call_info["name"]
                    dotted_parts = call_name.split(".")

                    if dotted_parts[0] in self.import_dict:  # ....... nltk.data?
                        dotted_parts = [self.import_dict[dotted_parts[0]]] + dotted_parts[1:]
                        call_name = ".".join(dotted_parts)

                    dotted_parts_import = call_name.split(".")
                    # if this function calls is from a imported module
                    for target_info in self.targets_info:
                        model_usage_call = self.importname
                        method_to_check = target_info['method_to_check']
                        class_to_check = target_info['class_to_check']

                        if dotted_parts[0] in self.all_options_import:  # not from variable, coming from modules
                            model_usage_call = self.importname
                        # if self.importname == "transformers":   #speechbrain
                        #     model_usage_call = method_to_check = target_info['method_to_check']
                        # print(model_usage_call)

                        if model_usage_call == dotted_parts_import[0]:
                            if method_to_check == 'hub.load' and model_usage_call == 'torch':
                                if 'hub' in dotted_parts_import[1:]:
                                    ind = dotted_parts_import.index('hub')
                                    if ind:
                                        if dotted_parts_import[ind+1] == 'load':
                                            call_data['name'] = call_name
                                            call_data['lineno'] = call_info['lineno']
                                            call_data['params'] = call_info['params']
                                            matched_result.append(call_data)

                            else:
                                if method_to_check in dotted_parts_import[1:]:
                                    # print(dotted_parts_import)
                                    ind = dotted_parts_import.index(method_to_check)

                                    if class_to_check == method_to_check:
                                        if ind == len(dotted_parts_import) - 1:
                                            class_to_check = ""
                                        else:
                                            ind = 0
                                    if (class_to_check == '' and ind) or (len(class_to_check) and class_to_check == dotted_parts_import[ind - 1]):
                                        # todo: direct mapping is possible
                                        call_data['name'] = call_name
                                        call_data['lineno'] = call_info['lineno']
                                        call_data['params'] = call_info['params']
                                        if model_usage_call == 'spacy':
                                            if "spacy.load" in call_name:
                                                matched_result.append(call_data)
                                        else:
                                            matched_result.append(call_data)
            except Exception as e:
                logging.error('exception in fetch_match_called: ' + str(e))
                traceback.print_exc()

        return matched_result, self.mnode.ast

    def fetch_caller(self, line):
        name_caller = ""
        try:
            defs_lineno_start = list(self.function_with_startln.keys())
            defs_lineno_end = list(self.function_with_endln.keys())
            # print(defs_lineno_start)
            # print(defs_lineno_end)

            if len(defs_lineno_end):
                defs_lineno_end.sort()
                defs_lineno_start.sort()

                if defs_lineno_start[0] < line <= defs_lineno_end[len(defs_lineno_end) - 1]:
                    # last_function_start = defs_lineno_start[len(defs_lineno_start) - 1]
                    start = self._find_val(defs_lineno_start, line)
                    name = self.function_with_startln[start]['name']
                    # make sure the endline
                    end_ln = self.function_with_startln[start]['end_linno']
                    if line <= end_ln:
                        name_caller = name

                    # if line > last_function_start:
                    #     name_caller = self.function_with_startln[last_function_start]
                    # else:
                    #     start = self._find_val(defs_lineno_start, line)
                    #     name_caller = self.function_with_startln[start]
        except Exception as e:
            logging.error('exception at fetch caller: ' + str(e))

        return name_caller

    # We want to check the variables in each  function
    def get_constant_assign(self):
        self.create_constant_propagation()

    # def get_vars(self):
    #     self.vars = self.mnode.parse_vars()

    def _resolve_param_scope(self, param, scope_name, line_call):
        op = ""
        # print(self.params_info)
        # print(param)
        if scope_name in self.params_info and len(scope_name) != 0:
            params_info_func = self.params_info[scope_name]

            values = [element for element in params_info_func if element['var'] == (param, 0)]
            if len(values):
                found_in_lines = {}
                for val in values:
                    found_in_lines[val['line']] = val['value']

                lines = list(found_in_lines.keys())
                # print(lines)

                value = self._find_val(lines, line_call)
                # print(value)
                # print(found_in_lines)

                op = found_in_lines[value]
                # print(op)

        return op

    def resolve_usage_param(self, param, func_def_name_caller, line_call):
        value = ""
        vars = self.mnode.parse_vars()

        # print('vars:')
        # print(vars)

        # if param not in vars:
        #     return param

        value = self._resolve_param_scope(param, func_def_name_caller, line_call)

        if value is None:
            return ""

        if len(value) == 0 and "." in func_def_name_caller:
            classname = func_def_name_caller.split(".")
            name_init = classname[0] + "__init__"
            value = self._resolve_param_scope(param, name_init, line_call)

            return value

        if len(value) == 0:
            value = self._resolve_param_scope(param, "global", line_call)
            return value

        if len(value) == 0:
            value = param
        #
        # print(param)
        # print(value)
        # print(line_call)

        return value

    def create_constant_propagation(self):
        cfg = self.mnode.gen_cfg()
        # print(cfg)
        #
        for (block_id, fun_name), fun_cfg in cfg.functioncfgs.items():
            m_ssa = SSA()
            # print(fun_name)
            ssa_results, const_dict = m_ssa.compute_SSA(fun_cfg)
            for name, value in const_dict.items():
                if isinstance(value, ast.Constant):
                    info = {'var': name, 'value': value.value, 'line': value.lineno}
                    if fun_name not in self.params_info:
                        self.params_info[fun_name] = []
                    self.params_info[fun_name].extend([info])

        const_dict = {}
        for class_cfg in cfg.class_cfgs.values():
            m_ssa = SSA()

            for (block_id, fun_name), func_cfg in class_cfg.functioncfgs.items():
                _, _dict = m_ssa.compute_SSA(func_cfg)
                for name, value in _dict.items():
                    if isinstance(value, ast.Constant):
                        info = {'var': name, 'value': value.value, 'line': value.lineno}
                        if fun_name not in self.params_info:
                            self.params_info[fun_name] = []
                        self.params_info[fun_name].extend([info])

        # full outer scope
        m_ssa = SSA()

        ssa_results, const_dict = m_ssa.compute_SSA(cfg)
        for name, value in const_dict.items():
            # print(name)
            if isinstance(value, ast.Constant):
                # print(value.value)
                info = {'var': name, 'value': value.value, 'line': value.lineno}
                if "global" not in self.params_info:
                    self.params_info['global'] = []
                self.params_info['global'].extend([info])

        # print(self.params_info)


    def fetch_called_and_imports(self):
        matched_result = []

        self.get_ast()
        # print(ast.dump(self.mnode.ast))
        if self.mnode.ast is None:
            logging.error('exception before 2to3 conversion: ' + self.blob)
        if self.mnode.ast is not None:
            self.get_imports()
            self.get_func_calls()
            # print(self.func_calls)
            try:
                for call_info in self.func_calls:
                    # print(call_info)
                    call_data = {}
                    call_name = call_info["name"]
                    dotted_parts = call_name.split(".")

                    if dotted_parts[0] in self.import_dict:  # ....... nltk.data?
                        dotted_parts = [self.import_dict[dotted_parts[0]]] + dotted_parts[1:]
                        call_name = ".".join(dotted_parts)

                    call_data['name'] = call_name
                    call_data['lineno'] = call_info['lineno']
                    call_data['params'] = call_info['params']
                    matched_result.append(call_data)
            except Exception as e:
                logging.error('exception in fetch_match_called: ' + str(e))
                traceback.print_exc()

        return matched_result

