
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

from collections import OrderedDict
import os
import re
from util import get_dict_path, AstVisitor, read_file_range

class Alert(OrderedDict):
    """A Redemption alert.

    An Alert is fundamentally an ordered dictionary.  This dictionary
    represents the JSON information for the alert.

    The main interfaces for an alert are the locate_repairable_node
    and attempt_patch methods.  locate_repairable_node is called with
    an AST node.  If the repair attempt succeeds it should return the
    AST node associated with the repair, otherwise it should return
    None.  attempt_patch is called on Alerts for which
    locate_repairable_node succeeded.  It should modify the JSON
    dictionary to apply an appropriate patch.  Data can be transmitted
    between the repair and patch processes by storing that data in
    attributes on the Alert instance.

    """

    def __init__(self, val):
        super().__init__(val)

    def filter_nodes(self, ast_node_list):
        """Filter the list of repairable AST nodes.

        From the given list of AST nodes, returns the list of nodes
        that this alert should consider for repair.  The list of nodes
        is assumed to be in order from shallowest to deepest in AST
        traversal order.
        """
        return ast_node_list

    def locate_repairable_node(self, astnode):
        """Determine if and how the expression `astnode` can be repaired.

        Return None if a repair cannot be attempted.  Return the AST
        node central to the repair if a repair can be attempted.  This
        function can modify alert state.
        """
        raise NotImplementedError

    def attempt_patch(self, context):
        """Add patch information to the alert that repairs the issue.

        Return True if the patch succeeded, False otherwise.
        """
        raise NotImplementedError

    @staticmethod
    def get_decl_of_var(node):
        if node["kind"] == "VarDecl":
            return node
        if node["kind"] != "DeclRefExpr":
            return None
        decl_id = node.get("referencedDecl",{}).get("prevId")
        if not decl_id:
            return None
        decl_node = node.var_decls_by_id.get(decl_id)
        if not decl_node:
            return None
        return decl_node

class NullAlert(Alert):
    """An alert that will not repair anything"""

    def locate_repairable_node(self, context):
        return None

    def attempt_patch(self, context):
        return False


class EXP33_C(Alert):
    """An alert that handles EXP33_C repairs"""

    def locate_repairable_node(self, context):
        def get_uninit_var_name(alert):
            message = alert.get("message","")
            m = re.match("variable '([^']*)' is not initialized", message)
            if m:
                return m.group(1)
            m = re.match("Uninitialized variable: (.*)", message)
            if m:
                return m.group(1)
            return None
        var = get_uninit_var_name(self)
        if var is None:
            return None
        decl_node = self.get_decl_of_var(context)
        if decl_node is None or decl_node.get("name") != var:
            return None
        # Don't try to initialize a variable declaration if it already has an initializer
        if "init" in decl_node:
            return None
        self.decl_node = decl_node
        self["ast_id"] = decl_node['id']
        return context

    def attempt_patch(self, context):
        # Create the patch
        init_val = "0"
        if self.decl_node["type"]["qualType"].endswith("*"):
            if self['file'].endswith(".cpp"):
                init_val = "nullptr"
            else:
                init_val = "NULL"
        elif self.decl_node["type"].get("desugaredQualType","").startswith(("struct","union")):
            init_val = "{}"
        try:
            loc = self.decl_node["loc"]
            byte_end = loc['offset'] + loc['tokLen']
        except KeyError:
            self["why_skipped"] = "AST node is missing VarDecl location information"
            self["patch"] = []
            return False
        edit = [self['file'], [
            [byte_end, byte_end, " = " + init_val]]]
        self["patch"] = [edit]
        return True

class MSC12_C(Alert):

    enable_msc12 = (os.getenv('REPAIR_MSC12') or "").lower() != "false"

    def try_deadinit_var_repair(self, node):
        message = self.get("message","")
        m = re.match("^Redundant initialization for '([^']*)'.*", message)
        if not m:
            return False
        decl_node = self.get_decl_of_var(node)
        if not decl_node:
            return False
        self.decl_node = decl_node
        self.repair_algo = "repair_deadinit_var"
        return True

    def locate_repairable_node(self, context):
        if not self.enable_msc12:
            self["ast_id"] = context['id']
            context.mark_skipped_alert(
                self,
                "Repair of MSC12-C is disabled; set env var REPAIR_MSC12=true to enable it.")
            return None

        if self.get("tool") == "cppcheck" and self.get("checker") == "unsignedLessThanZero":
            if context.get("opcode") == "<=":
                self.decl_node = context
                self.repair_algo = "unsigned_less_than"
                self["ast_id"] = context["id"]
                return context
            return None

        macro_file = get_dict_path(context, 'range', 'begin', 'spellingLoc', 'file')
        if macro_file is not None and macro_file.endswith("/assert.h"):
            context.mark_skipped_alert(self, "False positive: inside an assert")
            return None
        if self.try_deadinit_var_repair(context):
            self["ast_id"] = context['id']
            return context
        if self.get("checker", "").startswith("uselessAssignment"):
            if context["kind"] != "DeclRefExpr":
                return None
            self.decl_node = context.parent(True)
            if (self.decl_node["kind"] != "BinaryOperator"
                or not self.decl_node["opcode"].startswith("=")):
                return None
            self.repair_algo = "del_unused_asgn"
            self["ast_id"] = context['id']
            return context
        return None

    def attempt_patch(self, context):
        algo = self.repair_algo
        if algo == "repair_deadinit_var":
            try:
                filename = self.decl_node["range"]["file"]
                begin_offset = self.decl_node.get_begin()['offset']
                end = self.decl_node.get_end()
                end_offset = end["offset"] + end["tokLen"]
            except KeyError:
                self["why_skipped"] = "KeyError"
                self["patch"] = []
                return False
            decl_text = read_file_range(filename, begin_offset, end_offset)
            m = re.search(b"( *=)", decl_text)
            if not m:
                self["why_skipped"] = "No assignment '=' found!"
                self["patch"] = []
                return False
            del_start = begin_offset + m.span()[0]
            edit = [self['file'], [[del_start, end_offset, ""]]]
            self["patch"] = [edit]
            return self.decl_node
        elif algo == "del_unused_asgn":
            asgn = self.decl_node
            try:
                del_begin = self.decl_node.get_begin()["offset"]
                del_end = asgn["inner"][1].get_begin()["offset"]
            except KeyError:
                self["why_skipped"] = "KeyError"
                self["patch"] = []
                return False
            edit = [self['file'], [[del_begin, del_end, "(void) "]]]
            self["patch"] = [edit]
        elif algo == "unsigned_less_than":
            inner = self.decl_node["inner"]
            if len(inner) != 2:
                return None
            lhs, rhs = inner
            lhs_end = lhs.get_end()
            begin = lhs_end["offset"] + lhs_end["tokLen"]
            end = rhs.get_begin()["offset"]
            edit = [self['file'], [[begin, end, " == "]]]
            self["patch"] = [edit]
        else:
            return False


class EXP34_C(Alert):

    def get_error_handler_at_cursor(self, context):
        fn_decl_node = context.find_through_parents("FunctionDecl")
        try:
            eh = context.error_handling_db[fn_decl_node["id"]]["error_handler"]
            return eh
        except:
            return None

    def pointer_classifier(self, node):
        match node:
            case {'kind': "UnaryOperator", "opcode": "*"}:
                return 'pointer'
            case {'kind': "ArraySubscriptExpr"}:
                return 'array'
            case {'kind': "MemberExpr", "isArrow": True}:
                return 'member'
            # This case handles the part where rosecheckers marks calls to malloc
            case {'kind': "CallExpr"} if self.get("tool") == "rosecheckers":
                return 'call'
            case _:
                return False

    def pointer_filter(self, node):
        return bool(self.pointer_classifier(node))

    def line_pointer_filter(self):
        line = int(self['line'])
        def filter(node):
            return self.pointer_filter(node) and node.get_begin()['line'] == line
        return filter

    def find_dereference_locus(self, node):
        pf = self.pointer_filter if "column" in self else self.line_pointer_filter()
        pointers = filter(pf, node.traverse_descendants())
        context = None
        for item in pointers:
            context = item;
        return context

    def find_node(self, context):
        """Given an AST node, return the AST node that should be repaired.

        Returns None if a proper candidate node cannot be found.
        """
        if not self.whole_expr:
            return self.find_dereference_locus(context)
        elif not self.pointer_filter(context):
            return None
        return context

    def locate_repairable_node(self, context):
        if not self.whole_expr and "patch" in self:
            return None

        context = self.find_node(context)
        if context is None:
            return None
        self["ast_id"] = context['id']
        self.handle_error = self.get_error_handler_at_cursor(context)
        self.saved_whole_expr = self.whole_expr
        return context

    @staticmethod
    def is_expr_of_ptr_type(ast_node):
        # TODO: Should use the new type information once that is available
        return ast_node["type"]["qualType"].endswith("*")

    def attempt_patch(self, context):
        if context.get('opcode') == '*':
            ptr_subexpr = context['inner'][0]
        elif context["kind"] == "ArraySubscriptExpr":
            ptr_subexpr = context["inner"][0]
            if (not self.is_expr_of_ptr_type(ptr_subexpr)
                and self.is_expr_of_ptr_type(context["inner"][1])):
                ptr_subexpr = context["inner"][1]
        elif context["kind"] == "MemberExpr" and context.get("isArrow"):
            ptr_subexpr = context["inner"][0]
        elif context["kind"] == "CallExpr":
            ptr_subexpr = context
        elif self.whole_expr:
            ptr_subexpr = context
        else:
            raise Exception("Unrecognized node passed to add_null_check, and whole_expr isn't true.")

        byte_start = ptr_subexpr.get_begin()['offset'] - 1
        end = ptr_subexpr.get_end()
        byte_end = end['offset'] + end['tokLen']
        if self.handle_error is not None:
            closer = ", " + self.handle_error + ")"
        else:
            closer = ")"
        edit = [self['file'], [
            [byte_start+1, byte_start+1, "null_check("],
            [byte_end, byte_end, closer]]]
        self["patch"] = [edit]


class EXP34_C_CLANG_TIDY(EXP34_C):

    dereference_patterns = (
        [("member", re.compile(r"Access to field '(?P<field>[^']*)' results in a dereference of a null pointer \(loaded from variable '(?P<var>[^']*)\)")),
         ("array", re.compile(r"Array access (\(from variable '(?P<variable>[^']*')\)|\(via field '(?P<field>[^']*')\)) results in a null pointer dereference"))
         ]
    )

    def get_dereference_type(self):
        self.dereference_type = False
        msg = self.get("message", None)
        if msg is not None:
            for typ, pattern in self.dereference_patterns:
                if pattern.search(msg):
                    self.dereference_type = typ
                    break

    def pointer_filter(self, node):
        locus_type = self.pointer_classifier(node)
        if locus_type and self.dereference_type:
            return locus_type == self.dereference_type
        return bool(locus_type)

    def locate_repairable_node(self, node):
        self.get_dereference_type()
        return super().locate_repairable_node(node)

    def find_node(self, context):
        message = self.get("message","")
        m = re.match("Null pointer passed to ([0-9]+).. parameter", message)
        if m:
            if context.get("kind") != "CallExpr":
                return None
            param_ordinal = int(m.group(1))
            return context["inner"][param_ordinal]
        return super().find_node(context)


class EXP34_C_CPPCHECK(EXP34_C):

    def match_arithmetic_locus(self, context):
        match context:
            case {"kind": "BinaryOperator", "opcode": ("+" | "-"),
                  "type": { "qualType": typ },
                  "inner" : [{"type": {"qualType": ltyp}} as lhs,
                             {"type": {"qualType": rtyp}} as rhs]}:
                if typ[-1] != "*":
                    return None
                if typ == ltyp:
                    return lhs
                if typ == rtyp:
                    return rhs
                return None
            case {"kind": "UnaryOperator", "opcode": ("++" | "--"),
                  "type": { "qualType": typ },
                  "inner": [arg]}:
                if typ[-1] != "*":
                    return None
                return arg
            case _:
                return None

    def handle_null_pointer_redundant_check(self, context):
        if context.get('kind') == "DeclRefExpr":
            # This is a simple variable reference
            up = context.parent_non_implicit_cast()
            if up is None:
                return None
            if up.get('kind') == "CallExpr":
                # This variable reference is an argument to a function
                return context
        parent = context.parent(True)
        if (parent is not None
            and parent.get('kind') == "UnaryOperator"
            and parent.get('opcode') == "*"):
            # cppcheck points to the argument of a unary *.  This
            # handles that case
            return context
        return None

    def find_node(self, context):
        checker = self.get("checker")
        if checker == "nullPointerArithmeticRedundantCheck":
            return self.match_arithmetic_locus(context)
        elif checker == "nullPointerRedundantCheck":
            candidate = self.handle_null_pointer_redundant_check(context)
            if candidate is not None:
                return candidate
        return super().find_node(context)

class EXP34_C_ROSECHECKERS(EXP34_C):

    def locate_repairable_node(self, context):
        if "ast_id" in self and getattr(self, 'saved_whole_expr', False):
            return None
        return super().locate_repairable_node(context)


class CWE_476(EXP34_C):
    # Currently an exact duplicate of EXP34_C
    pass

class CWE_561(MSC12_C):
    # Currently an exact duplicate of MSC12_C
    pass


# Rule list is a map of rule names to a list of candidates.  Each
# candidate is a pair of a match dictionary and an alert class.  A
# match dictionary is a map of alert keys to regular expression
# strings.
rule_list = {
    "CWE-476": [({}, CWE_476)],
    "CWE-561": [({}, CWE_561)],
    "EXP33-C": [({}, EXP33_C)],
    "EXP34-C": [({"tool": "cppcheck"}, EXP34_C_CPPCHECK),
                ({"tool": "clang-tidy"}, EXP34_C_CLANG_TIDY),
                ({"tool": "rosecheckers"}, EXP34_C_ROSECHECKERS),
                ({}, EXP34_C)],
    "MSC12-C": [({}, MSC12_C)]
}


def alert_from_dict(json_alert):
    rule = json_alert.get("rule")
    candidate = rule_list.get(rule, [({}, NullAlert)])
    for (option, cls) in candidate:
        found = cls
        for k, v in option.items():
            val = json_alert.get(k)
            if val is None or re.search(v, val) is None:
                found = None
                break
        if found is not None:
            return found(json_alert)
    return NullAlert(json_alert)



