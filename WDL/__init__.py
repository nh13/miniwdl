# pyre-strict
import lark
import inspect
import WDL._parser
from WDL import Expr as E
import WDL.StdLib

def sp(meta) -> E.SourcePosition:
    return E.SourcePosition(line=meta.line, column=meta.column,
                            end_line=meta.end_line, end_column=meta.end_column)

# Transformer from lark.Tree to WDL.Expr
class _ExprTransformer(lark.Transformer):

    def boolean_true(self, items, meta) -> E.Base:
        assert items == []
        return E.Boolean(sp(meta), True)
    def boolean_false(self, items, meta) -> E.Base:
        assert items == []
        return E.Boolean(sp(meta), False)
    def int(self, items, meta) -> E.Base:
        assert len(items) == 1
        return E.Int(sp(meta), int(items[0]))
    def float(self, items, meta) -> E.Base:
        assert len(items) == 1
        return E.Float(sp(meta), float(items[0]))
    def array(self, items, meta) -> E.Base:
        return E.Array(sp(meta), items)

    def apply(self, items, meta) -> E.Base:
        assert len(items) >= 1
        return E.Apply(sp(meta), items[0], items[1:])
    def negate(self, items, meta) -> E.Base:
        return E.Apply(sp(meta), "_negate", items)
    def get(self, items, meta) -> E.Base:
        return E.Apply(sp(meta), "_get", items)

    def ifthenelse(self, items, meta) -> E.Base:
        return E.IfThenElse(sp(meta), items)

# have lark pass the 'meta' with line/column numbers to each transformer method
for name, method in inspect.getmembers(_ExprTransformer, inspect.isfunction):
    if not name.startswith('_'):
        setattr(_ExprTransformer, name, lark.v_args(meta=True)(method))

# _ExprTransformer infix operators        
for op in ["land", "lor", "add", "sub", "mul", "div", "rem",
           "eqeq", "neq", "lt", "lte", "gt", "gte"]:
    def fn(self, items, meta, op=op):
        assert len(items) == 2
        return E.Apply(sp(meta), "_"+op, items)
    setattr(_ExprTransformer, op, lark.v_args(meta=True)(classmethod(fn)))

def parse_expr(txt : str) -> E.Base:
    return _ExprTransformer().transform(WDL._parser.parse(txt))