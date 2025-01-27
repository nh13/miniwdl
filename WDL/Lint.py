# pylint: disable=protected-access
"""
Annotate WDL document AST with hygiene warnings (underlies ``miniwdl check``)

Given a ``doc: WDL.Document``, the lint warnings can be retrieved like so::

    lint = WDL.Lint.collect(WDL.Lint.lint(doc, descend_imports=False))
    for (pos, lint_class, message) in lint:
        assert isinstance(pos, WDL.SourcePosition)
        assert isinstance(lint_class, str) and isinstance(message, str)
        print(json.dumps({
            "filename"   : pos.filename,
            "line"       : pos.line,
            "end_line"   : pos.end_line,
            "column"     : pos.column,
            "end_column" : pos.end_column,
            "lint"       : lint_class,
            "message"    : message,
        }))

The ``descend_imports`` flag controls whether lint warnings are generated for imported documents
recursively (true, default), or otherwise only the given document (false).
"""
import subprocess
import tempfile
import json
import os
import shutil
from typing import Any, Optional, Union
from . import Error, Type, Env, Expr, Tree, StdLib, Walker, _util


class Linter(Walker.Base):
    """
    Linters are Walkers which annotate each Tree node with
        ``lint : List[Tuple[SourceNode,str,str]]``
    providing lint warnings with a node (possibly more-specific than the
    node it's attached to), short codename, and message.

    Linters initialize the base Walker with ``auto_descend=True`` by default,
    but this can be overridden if control of recursive descent is needed.
    """

    def __init__(self, auto_descend: bool = True, descend_imports: bool = True):
        super().__init__(auto_descend=auto_descend, descend_imports=descend_imports)

    def add(self, obj: Error.SourceNode, message: str, pos: Optional[Error.SourcePosition] = None):
        """
        Used by subclasses to attach lint to a node.

        Note, lint attaches to Tree nodes (Decl, Task, Workflow, Scatter,
        Conditional, Document). Warnings about individual expressions should
        attach to their parent Tree node.
        """
        assert not isinstance(obj, Expr.Base)
        if pos is None:
            pos = obj.pos
        if not hasattr(obj, "lint"):
            obj.lint = []
        obj.lint.append((pos, self.__class__.__name__, message))


_all_linters = []


def a_linter(cls):
    """
    Decorator for subclasses of ``Linter`` to register them for use
    """
    _all_linters.append(cls)


def lint(doc, descend_imports: bool = True):
    """
    Apply all linters to the document
    """

    # Add additional markups to the AST for use by the linters
    Walker.SetParents()(doc)
    Walker.MarkCalled()(doc)
    Walker.SetReferrers()(doc)

    # instantiate linters
    linter_instances = [cons(descend_imports=descend_imports) for cons in _all_linters]

    # run auto-descend linters "concurrently"
    Walker.Multi(
        [linter for linter in linter_instances if linter.auto_descend],
        descend_imports=descend_imports,
    )(doc)
    # run each non-auto-descend linter
    for linter in linter_instances:
        if not linter.auto_descend:
            linter(doc)

    return doc


class _Collector(Walker.Base):
    def __init__(self):
        super().__init__(auto_descend=True)
        self.lint = []

    def __call__(self, obj):
        if hasattr(obj, "lint"):
            self.lint.extend(getattr(obj, "lint"))
        super().__call__(obj)


def collect(doc):
    """
    Recursively traverse the already-linted document and collect a flat list of
    (tree node, linter_class, message)
    """
    collector = _Collector()
    collector(doc)
    return collector.lint


def _find_input_decl(obj: Tree.Call, name: str) -> Tree.Decl:
    assert isinstance(obj.callee, (Tree.Task, Tree.Workflow))
    return Env.resolve(obj.callee.available_inputs, [], name)


def _compound_coercion(to_type, from_type, base_to_type, extra_from_type=None):
    # helper for StringCoercion and FileCoercion to detect coercions implied
    # within compound types like arrays
    if isinstance(to_type, Type.Array) and isinstance(from_type, Type.Array):
        return _compound_coercion(
            to_type.item_type, from_type.item_type, base_to_type, extra_from_type
        )
    elif isinstance(to_type, Type.Map) and isinstance(from_type, Type.Map):
        return _compound_coercion(
            to_type.item_type[0], from_type.item_type[0], base_to_type, extra_from_type
        ) or _compound_coercion(
            to_type.item_type[1], from_type.item_type[1], base_to_type, extra_from_type
        )
    elif isinstance(to_type, Type.Pair) and isinstance(from_type, Type.Pair):
        return _compound_coercion(
            to_type.left_type, from_type.left_type, base_to_type, extra_from_type
        ) or _compound_coercion(
            to_type.right_type, from_type.right_type, base_to_type, extra_from_type
        )
    elif isinstance(to_type, base_to_type):
        if extra_from_type:
            return not isinstance(from_type, (base_to_type, extra_from_type, Type.Any))
        return not isinstance(from_type, (base_to_type, Type.Any))
    return False


def _parent_executable(obj: Error.SourceNode) -> Optional[Union[Tree.Task, Tree.Workflow]]:
    if isinstance(obj, (Tree.Task, Tree.Workflow)):
        return obj
    if hasattr(obj, "parent_executable"):
        return getattr(obj, "parent_executable")
    if hasattr(obj, "parent"):
        ans = _parent_executable(getattr(obj, "parent"))
        setattr(obj, "parent_executable", ans)
        return ans
    return None


@a_linter
class StringCoercion(Linter):
    # String declaration with non-String rhs expression
    # File-to-String coercions are normal in tasks, but flagged at the workflow level.

    def decl(self, obj: Tree.Decl) -> Any:
        if obj.expr and _compound_coercion(
            obj.type,
            obj.expr.type,
            Type.String,
            (Type.File if isinstance(_parent_executable(obj), Tree.Task) else None),
        ):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def expr(self, obj: Expr.Base) -> Any:
        pt = getattr(obj, "parent")
        if isinstance(obj, Expr.Apply):
            # String function operands with non-String expression
            if obj.function_name == "_add":
                any_string = False
                all_string = True
                for arg in obj.arguments:
                    if isinstance(arg.type, Type.String):
                        any_string = True
                    elif not isinstance(arg.type, Type.File):
                        all_string = arg.type
                if any_string and all_string is not True and not isinstance(pt, Tree.Task):
                    # exception when parent is Task (i.e. we're in the task
                    # command) because the coercion is probably intentional
                    self.add(
                        pt,
                        "string concatenation (+) has {} argument".format(str(all_string)),
                        obj.pos,
                    )
            else:
                F = getattr(StdLib.Base(), obj.function_name)
                if isinstance(F, StdLib.StaticFunction) and obj.function_name != "basename":
                    # ok for basename to take either String or File
                    for i in range(min(len(F.argument_types), len(obj.arguments))):
                        F_i = F.argument_types[i]
                        arg_i = obj.arguments[i]
                        if _compound_coercion(
                            F_i,
                            arg_i.type,
                            Type.String,
                            (Type.File if isinstance(_parent_executable(obj), Tree.Task) else None),
                        ):
                            msg = "{} argument of {}() = :{}:".format(
                                str(F_i), F.name, str(arg_i.type)
                            )
                            self.add(pt, msg, arg_i.pos)
        elif isinstance(obj, Expr.Array):
            # Array literal with mixed item types, one of which is String,
            # causing coercion of the others
            any_string = False
            all_string = True
            item_types = []
            for elt in obj.items:
                if isinstance(elt.type, Type.String):
                    any_string = True
                elif not isinstance(elt.type, (Type.File, Type.Any)):
                    all_string = False
                item_types.append(str(elt.type))
            if any_string and not all_string:
                msg = "{} literal = [{}]".format(
                    str(obj.type), ", ".join(":{}:".format(ty) for ty in item_types)
                )
                self.add(pt, msg, obj.pos)

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _compound_coercion(decl.type, inp_expr.type, Type.String):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


@a_linter
class FileCoercion(Linter):
    # String-to-File coercions are typical in task outputs, but potentially
    # problematic elsewhere.

    def __init__(self, descend_imports: bool = True):
        super().__init__(auto_descend=False, descend_imports=descend_imports)

    def task(self, obj: Tree.Task) -> Any:
        # descend into everything but outputs
        for d in obj.inputs or []:
            self(d)
        for d in obj.postinputs:
            self(d)
        self(obj.command)
        for _, ex in obj.runtime.items():
            self(ex)

    # File declaration with String rhs expression
    def decl(self, obj: Tree.Decl) -> Any:
        super().decl(obj)
        if obj.expr and _compound_coercion(obj.type, obj.expr.type, Type.File):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def expr(self, obj: Expr.Base) -> Any:
        super().expr(obj)
        pt = getattr(obj, "parent")
        if isinstance(obj, Expr.Apply):
            # File function operands with String expression
            F = getattr(StdLib.Base(), obj.function_name)
            if isinstance(F, StdLib.StaticFunction):
                for i in range(min(len(F.argument_types), len(obj.arguments))):
                    F_i = F.argument_types[i]
                    arg_i = obj.arguments[i]
                    if _compound_coercion(F_i, arg_i.type, Type.File):
                        msg = "{} argument of {}() = :{}:".format(str(F_i), F.name, str(arg_i.type))
                        self.add(pt, msg, arg_i.pos)
            elif obj.function_name == "size":
                arg0ty = obj.arguments[0].type
                if not isinstance(arg0ty, Type.File) and not (
                    isinstance(arg0ty, Type.Array) and isinstance(arg0ty.item_type, Type.File)
                ):
                    self.add(
                        pt,
                        "File?/Array[File?] argument of size() = :{}:".format(
                            str(obj.arguments[0].type)
                        ),
                        obj.arguments[0].pos,
                    )

    def call(self, obj: Tree.Call) -> Any:
        super().call(obj)
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _compound_coercion(decl.type, inp_expr.type, Type.File):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


def _array_levels(ty: Type.Base, l=0):
    if isinstance(ty, Type.Array):
        return _array_levels(ty.item_type, l + 1)
    return l


def _is_array_coercion(value_type: Type.Base, expr_type: Type.Base):
    return (
        isinstance(value_type, Type.Array)
        and _array_levels(value_type) > _array_levels(expr_type)
        and not isinstance(expr_type, Type.Any)
        and expr_type != Type.Array(Type.Any())
    )


@a_linter
class ArrayCoercion(Linter):
    # implicit promotion of T to Array[T]
    def decl(self, obj: Tree.Decl) -> Any:
        if obj.expr and _is_array_coercion(obj.type, obj.expr.type):
            msg = "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type))
            self.add(obj, msg)

    def expr(self, obj: Expr.Base) -> Any:
        pt = getattr(obj, "parent")
        if isinstance(obj, Expr.Apply):
            F = getattr(StdLib.Base(), obj.function_name)
            if isinstance(F, StdLib.StaticFunction):
                for i in range(min(len(F.argument_types), len(obj.arguments))):
                    F_i = F.argument_types[i]
                    arg_i = obj.arguments[i]
                    if _is_array_coercion(F_i, arg_i.type):
                        msg = "{} argument of {}() = :{}:".format(str(F_i), F.name, str(arg_i.type))
                        self.add(pt, msg, arg_i.pos)

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _is_array_coercion(decl.type, inp_expr.type):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


@a_linter
class OptionalCoercion(Linter):
    # Expression of optional type where a non-optional value is expected
    # Normally these fail typechecking, but the enforcement isn't stringent in
    # older WDLs.
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply):
            if obj.function_name in ["_add", "_sub", "_mul", "_div", "_land", "_lor"]:
                assert len(obj.arguments) == 2
                arg0ty = obj.arguments[0].type
                arg1ty = obj.arguments[1].type
                if (arg0ty.optional or arg1ty.optional) and (
                    obj.function_name != "_add" or not isinstance(getattr(obj, "parent"), Tree.Task)
                ):
                    # exception for + in task command because the coercion is
                    # probably intentional, per "Prepending a String to an
                    # Optional Parameter"
                    # TODO: carve out an exception for the pattern
                    #          if defined(x) then EXPR_WITH_x else DEFAULT
                    self.add(
                        getattr(obj, "parent"),
                        "infix operator has :{}: and :{}: operands".format(
                            str(arg0ty), str(arg1ty)
                        ),
                    )
            else:
                F = getattr(StdLib.Base(), obj.function_name)
                if isinstance(F, StdLib.StaticFunction):
                    for i in range(min(len(F.argument_types), len(obj.arguments))):
                        F_i = F.argument_types[i]
                        arg_i = obj.arguments[i]
                        if not arg_i.type.coerces(F_i, check_quant=True) and not _is_array_coercion(
                            F_i, arg_i.type
                        ):
                            msg = "{} argument of {}() = :{}:".format(
                                str(F.argument_types[i]), F.name, str(obj.arguments[i].type)
                            )
                            self.add(getattr(obj, "parent"), msg, obj.arguments[i].pos)

    def decl(self, obj: Tree.Decl) -> Any:
        if (
            obj.expr
            and not obj.expr.type.coerces(obj.type, check_quant=True)
            and not _is_array_coercion(obj.type, obj.expr.type)
        ):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if not inp_expr.type.coerces(decl.type, check_quant=True) and not _is_array_coercion(
                decl.type, inp_expr.type
            ):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


def _is_nonempty_coercion(value_type: Type.Base, expr_type: Type.Base):
    return (
        isinstance(value_type, Type.Array)
        and isinstance(expr_type, Type.Array)
        and value_type.nonempty
        and not expr_type.nonempty
    )
    # TODO: descend into compound types


@a_linter
class NonemptyCoercion(Linter):
    # An array of possibly-empty type where a nonempty array is expected
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Apply):
            F = getattr(StdLib.Base(), obj.function_name)
            if isinstance(F, StdLib.StaticFunction):
                for i in range(min(len(F.argument_types), len(obj.arguments))):
                    F_i = F.argument_types[i]
                    arg_i = obj.arguments[i]
                    if _is_nonempty_coercion(F_i, arg_i.type):
                        msg = "{} argument of {}() = :{}:".format(
                            str(F.argument_types[i]), F.name, str(obj.arguments[i].type)
                        )
                        self.add(getattr(obj, "parent"), msg, obj.arguments[i].pos)

    def decl(self, obj: Tree.Decl) -> Any:
        # heuristic exception for: Array[File]+ outp = glob(...)
        if (
            obj.expr
            and _is_nonempty_coercion(obj.type, obj.expr.type)
            and not (
                isinstance(obj.expr, Expr.Apply)
                and obj.expr.function_name in ["glob", "read_lines", "read_tsv", "read_array"]
            )
        ):
            self.add(obj, "{} {} = :{}:".format(str(obj.type), obj.name, str(obj.expr.type)))

    def call(self, obj: Tree.Call) -> Any:
        for name, inp_expr in obj.inputs.items():
            decl = _find_input_decl(obj, name)
            if _is_nonempty_coercion(decl.type, inp_expr.type):
                msg = "input {} {} = :{}:".format(str(decl.type), decl.name, str(inp_expr.type))
                self.add(obj, msg, inp_expr.pos)


@a_linter
class IncompleteCall(Linter):
    # Call without all required inputs (allowed for top-level workflow)
    def call(self, obj: Tree.Call) -> Any:
        assert obj.callee is not None
        # pyre-fixme
        required_inputs = set(decl.name for decl in obj.callee.required_inputs)
        for name, _ in obj.inputs.items():
            if name in required_inputs:
                required_inputs.remove(name)
        if required_inputs:
            msg = "required input(s) omitted in call to {} ({})".format(
                obj.callee.name, ", ".join(required_inputs)
            )
            self.add(obj, msg)


@a_linter
class NameCollision(Linter):
    # Name collisions between
    # - call and import
    # - call and its containing workflow
    # - call and struct type/alias
    # - decl and import
    # - decl and workflow
    # - decl and task
    # - decl and struct type/alias
    # - scatter variable and import
    # - scatter variable and workflow
    # - scatter variable and task
    # - scatter variable and struct type/alias
    # - workflow and import
    # - workflow and struct type/alias
    # - task and import
    # - task and struct type/alias
    # - struct type/alias and import
    # These are allowed, but confusing.
    def call(self, obj: Tree.Call) -> Any:
        doc = obj
        while not isinstance(doc, Tree.Document):
            doc = getattr(doc, "parent")
        assert isinstance(doc, Tree.Document)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "call name '{}' collides with imported document namespace".format(obj.name)
                self.add(obj, msg)
        if doc.workflow and doc.workflow.name == obj.name:
            msg = "call name '{}' collides with workflow name".format(obj.name)
            self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.rhs, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "call name '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.rhs.imported else ""
                )
                self.add(obj, msg)

    def decl(self, obj: Tree.Decl) -> Any:
        doc = obj
        while not isinstance(doc, Tree.Document):
            doc = getattr(doc, "parent")
        assert isinstance(doc, Tree.Document)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "declaration of '{}' collides with imported document namespace".format(
                    obj.name
                )
                self.add(obj, msg)
        if doc.workflow and doc.workflow.name == obj.name:
            msg = "declaration of '{}' collides with workflow name".format(obj.name)
            self.add(obj, msg)
        for task in doc.tasks:
            if obj.name == task.name:
                msg = "declaration of '{}' collides with a task name".format(obj.name)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.rhs, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "declaration of '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.rhs.imported else ""
                )
                self.add(obj, msg)

    def scatter(self, obj: Tree.Scatter) -> Any:
        doc = obj
        while not isinstance(doc, Tree.Document):
            doc = getattr(doc, "parent")
        assert isinstance(doc, Tree.Document)
        for imp in doc.imports:
            if imp.namespace == obj.variable:
                msg = "scatter variable '{}' collides with imported document namespace".format(
                    obj.variable
                )
                self.add(obj, msg)
        if doc.workflow and doc.workflow.name == obj.variable:
            msg = "scatter variable '{}' collides with workflow name".format(obj.variable)
            self.add(obj, msg)
        for task in doc.tasks:
            if obj.variable == task.name:
                msg = "scatter variable '{}' collides with a task name".format(obj.variable)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.rhs, Tree.StructTypeDef)
            if stb.name == obj.variable:
                msg = "scatter variable '{}' colides with {}struct type".format(
                    obj.variable, "imported " if stb.rhs.imported else ""
                )
                self.add(obj, msg)

    def workflow(self, obj: Tree.Workflow) -> Any:
        doc = obj
        while not isinstance(doc, Tree.Document):
            doc = getattr(doc, "parent")
        assert isinstance(doc, Tree.Document)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "workflow name '{}' collides with imported document namespace".format(
                    obj.name
                )
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.rhs, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "workflow name '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.rhs.imported else ""
                )
                self.add(obj, msg)

    def task(self, obj: Tree.Task) -> Any:
        doc = obj
        while not isinstance(doc, Tree.Document):
            doc = getattr(doc, "parent")
        assert isinstance(doc, Tree.Document)
        for imp in doc.imports:
            if imp.namespace == obj.name:
                msg = "task name '{}' collides with imported document namespace".format(obj.name)
                self.add(obj, msg)
        for stb in doc.struct_typedefs:
            assert isinstance(stb, Env.Binding) and isinstance(stb.rhs, Tree.StructTypeDef)
            if stb.name == obj.name:
                msg = "task name '{}' colides with {}struct type".format(
                    obj.name, "imported " if stb.rhs.imported else ""
                )
                self.add(obj, msg)

    def document(self, obj: Tree.Document) -> Any:
        for imp in obj.imports:
            for stb in obj.struct_typedefs:
                assert isinstance(stb, Env.Binding) and isinstance(stb.rhs, Tree.StructTypeDef)
                if stb.name == imp.namespace:
                    msg = "imported document namespace '{}' collides with {}struct type".format(
                        imp.namespace, "imported " if stb.rhs.imported else ""
                    )
                    self.add(obj, msg)


@a_linter
class UnusedImport(Linter):
    # Nothing used from an imported document
    # TODO: suppress if document is imported just to use its struct type declarations
    def document(self, obj: Tree.Document) -> Any:
        for imp in obj.imports:
            assert imp.doc is not None
            any_called = False
            for task in imp.doc.tasks:
                if getattr(task, "called", False):
                    any_called = True
            if imp.doc.workflow and getattr(imp.doc.workflow, "called", False):
                any_called = True
            if not any_called and (imp.doc.tasks or imp.doc.workflow):
                self.add(
                    obj, "no calls to tasks/workflow in the imported document " + imp.namespace
                )


@a_linter
class ForwardReference(Linter):
    # Ident referencing a value or call output lexically precedes Decl/Call
    def expr(self, obj: Expr.Base) -> Any:
        if isinstance(obj, Expr.Ident):
            referee = obj.referee
            while isinstance(referee, Tree.Gather):
                referee = referee.referee
            if isinstance(referee, (Tree.Decl, Tree.Call)) and (
                referee.pos.line > obj.pos.line
                or (referee.pos.line == obj.pos.line and referee.pos.column > obj.pos.column)
            ):
                if isinstance(referee, Tree.Decl):
                    msg = "reference to {} precedes its declaration".format(obj.name)
                elif isinstance(referee, Tree.Call):
                    msg = "reference to output of {} precedes the call".format(
                        ".".join(obj.namespace)
                    )
                else:
                    assert False
                self.add(getattr(obj, "parent"), msg, obj.pos)


@a_linter
class UnusedDeclaration(Linter):
    # Nothing references a (non-input) Decl
    def decl(self, obj: Tree.Decl) -> Any:
        pt = getattr(obj, "parent")
        is_output = (
            isinstance(pt, (Tree.Workflow, Tree.Task))
            and getattr(pt, "outputs")
            and obj in getattr(pt, "outputs")
        )
        if not is_output and not getattr(obj, "referrers", []):
            # heuristic exceptions:
            # 1. File whose name suggests it's an hts index file; as these
            #    commonly need to be localized, but not explicitly used in task
            #    command
            # 2. dxWDL "native" task stubs, which declare inputs but leave
            #    command empty.
            index_suffixes = [
                "index",
                "indexes",
                "indices",
                "idx",
                "tbi",
                "bai",
                "crai",
                "csi",
                "fai",
                "dict",
            ]
            if not (
                (
                    isinstance(obj.type, Type.File)
                    and sum(1 for sfx in index_suffixes if obj.name.lower().endswith(sfx))
                )
                or (
                    isinstance(obj.type, Type.Array)
                    and isinstance(obj.type.item_type, Type.File)
                    and sum(1 for sfx in index_suffixes if obj.name.lower().endswith(sfx))
                )
                or (
                    isinstance(pt, Tree.Task)
                    and pt.meta.get("type") == "native"
                    and pt.meta.get("id")
                )
            ):
                self.add(obj, "nothing references {} {}".format(str(obj.type), obj.name))


@a_linter
class UnusedCall(Linter):
    # the outputs of a Call are neither used nor propagated

    def call(self, obj: Tree.Call) -> Any:
        if obj.effective_outputs and not getattr(obj, "referrers", []):
            workflow = obj
            while not isinstance(workflow, Tree.Workflow):
                workflow = getattr(workflow, "parent")
            assert isinstance(workflow, Tree.Workflow)
            if workflow.outputs is not None:
                self.add(
                    obj,
                    "nothing references the outputs of the call "
                    + obj.name
                    + " nor are are they output from the workflow "
                    + workflow.name,
                )


@a_linter
class UnnecessaryQuantifier(Linter):
    # A declaration like T? x = :T: where the right-hand side can't be null.
    # The optional quantifier is unnecessary except within a task/workflow
    # input section (where it denotes that the default value can be overridden
    # by expressly passing null)

    def decl(self, obj: Tree.Decl) -> Any:
        if obj.type.optional and obj.expr and not obj.expr.type.optional:
            tw = obj
            while not isinstance(tw, (Tree.Task, Tree.Workflow)):
                tw = getattr(tw, "parent")
            assert isinstance(tw, (Tree.Task, Tree.Workflow))
            if isinstance(tw.inputs, list) and obj not in tw.inputs:
                self.add(
                    obj,
                    "unnecessary optional quantifier (?) for non-input {} {}".format(
                        obj.type, obj.name
                    ),
                )


_shellcheck_available = None


@a_linter
class CommandShellCheck(Linter):
    # If ShellCheck is installed, run it on the task command and propagate any
    # additional lint it finds.

    # we suppress
    #   SC1083 This {/} is literal
    #   SC2043 This loop will only ever run once for a constant value
    #   SC2050 This expression is constant
    #   SC2157 Argument to -n is always true due to literal strings
    #   SC2193 The arguments to this comparison can never be equal
    # which can be triggered by dummy values we substitute to write the script
    # also SC1009 and SC1072 are non-informative commentary
    _suppressions = [1009, 1072, 1083, 2043, 2050, 2157, 2193]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tmpdir = tempfile.mkdtemp(prefix="miniwdl_shellcheck_")
        global _shellcheck_available
        if _shellcheck_available is None:
            _shellcheck_available = shutil.which("shellcheck") is not None

    def __del__(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def task(self, obj: Tree.Task) -> Any:
        global _shellcheck_available
        if not _shellcheck_available:
            return

        # for each expression placeholder in the command, make up a dummy value
        # of the appropriate type that shouldn't trigger shellcheck
        command = []
        for part in obj.command.parts:
            if isinstance(part, Expr.Placeholder):
                command.append(_shellcheck_dummy_value(part.expr.type, part.pos))
            else:
                assert isinstance(part, str)
                command.append(part)
        col_offset, command = _util.strip_leading_whitespace("".join(command))

        # write out a temp file with this fake script
        tfn = os.path.join(self._tmpdir, obj.name)
        with open(tfn, "w") as outfile:
            outfile.write(command)

        # run shellcheck on it & collect JSON results
        shellcheck_items = None
        try:
            shellcheck_items = subprocess.check_output(
                [
                    "shellcheck",
                    "-s",
                    "bash",
                    "-f",
                    "json",
                    "-e",
                    ",".join(str(c) for c in self.__class__._suppressions),
                    tfn,
                ]
            )
        except subprocess.CalledProcessError as cpe:
            if cpe.returncode in (0, 1):
                shellcheck_items = cpe.stdout
            else:
                self.add(
                    obj,
                    "shellcheck failed on the task command; update shellcheck version or use --no-shellcheck to suppress this warning",
                    obj.command.pos,
                )

        if shellcheck_items:
            try:
                shellcheck_items = json.loads(shellcheck_items)
                assert isinstance(shellcheck_items, list)

                # annotate on tree, adding appropriate offsets to line/column positions
                for item in shellcheck_items:
                    line = obj.command.pos.line + item["line"] - 1
                    column = col_offset + item["column"] - 1
                    self.add(
                        obj,
                        "SC{} {}".format(item["code"], item["message"]),
                        Error.SourcePosition(
                            filename=obj.command.pos.filename,
                            line=line,
                            column=column,
                            end_line=line,
                            end_column=column,
                        ),
                    )
            except:
                self.add(
                    obj,
                    "error parsing shellcheck output JSON; update shellcheck version or use --no-shellcheck to suppress this warning",
                    obj.command.pos,
                )


def _shellcheck_dummy_value(ty, pos):
    if isinstance(ty, Type.Array):
        return _shellcheck_dummy_value(ty.item_type, pos)
    if isinstance(ty, Type.Boolean):
        return "false"
    # estimate the length of the interpolation in the original source, so that
    # shellcheck will see the same column numbers. + 3 accounts for "~{" and "}"
    desired_length = max(1, pos.end_column - pos.column) + 3
    if isinstance(ty, (Type.Int, Type.Float)):
        return "4" * desired_length
    # assert ty.coerces(Type.String), str(ty)
    # https://github.com/HumanCellAtlas/skylab/blob/a99b8ddffdb3c0ebdea1a8905d28f01a4d365af5/pipelines/10x/count/count.wdl#L325
    # https://github.com/openwdl/wdl/blob/master/versions/draft-2/SPEC.md#map-serialization
    return "x" * desired_length


@a_linter
class MixedIndentation(Linter):
    # Line of task command mixes tab and space indentation
    def task(self, obj: Tree.Task) -> Any:
        command_lines = "".join(
            (s if isinstance(s, str) else "$") for s in obj.command.parts
        ).split("\n")
        for ofs, line in enumerate(command_lines):
            indentation = line[: (len(line) - len(line.lstrip()))]
            if " " in indentation and "\t" in indentation:
                self.add(
                    obj,
                    "command indented with both spaces & tabs",
                    Error.SourcePosition(
                        filename=obj.command.pos.filename,
                        line=obj.command.pos.line + ofs,
                        column=1,
                        end_line=obj.command.pos.line + ofs,
                        end_column=len(line),
                    ),
                )
                break


@a_linter
class SelectArray(Linter):
    # application of select_first or select_all on a non-optional array
    def expr(self, obj: Expr.Base) -> Any:
        pt = getattr(obj, "parent")
        if isinstance(obj, Expr.Apply) and obj.function_name in ["select_first", "select_all"]:
            arg0 = obj.arguments[0]
            if isinstance(arg0.type, Type.Array) and not arg0.type.item_type.optional:
                self.add(
                    pt,
                    "array of non-optional items passed to " + obj.function_name,
                    obj.arguments[0].pos,
                )


@a_linter
class UnknownRuntimeKey(Linter):

    # refs:
    # https://cromwell.readthedocs.io/en/develop/RuntimeAttributes/
    # https://github.com/broadinstitute/cromwell/blob/develop/wom/src/main/scala/wom/RuntimeAttributes.scala
    # https://github.com/broadinstitute/cromwell/blob/develop/supportedBackends/google/pipelines/common/src/main/scala/cromwell/backend/google/pipelines/common/PipelinesApiRuntimeAttributes.scala
    # https://github.com/broadinstitute/cromwell/blob/develop/supportedBackends/aws/src/main/scala/cromwell/backend/impl/aws/AwsBatchRuntimeAttributes.scala
    # https://github.com/openwdl/wdl/pull/315
    # https://github.com/dnanexus/dxWDL/blob/master/doc/ExpertOptions.md
    known_keys = set(
        [
            "bootDiskSizeGb",
            "container",
            "continueOnReturnCode",
            "cpu",
            "cpuPlatform",
            "disks",
            "docker",
            "dx_instance_type",
            "gpu",
            "gpuCount",
            "gpuType",
            "maxRetries",
            "memory",
            "noAddress",
            "preemptible",
            "queueArn",
            "returnCodes",
            "time",
            "zones",
        ]
    )

    def task(self, obj: Tree.Task) -> Any:
        for k in obj.runtime:
            if k not in self.known_keys:
                self.add(obj, "unknown entry in task runtime section: " + k, obj.runtime[k].pos)
