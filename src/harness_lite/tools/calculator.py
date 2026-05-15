"""计算器工具"""

import ast
import operator
from typing import Dict, Any

from harness_lite.tools.base import BaseTool


class CalculatorTool(BaseTool):
    """数学计算工具"""

    # 支持的运算符
    OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return "执行数学计算，支持加减乘除、括号、幂运算等"

    def execute(self, expression: str) -> str:
        """
        执行数学计算

        Args:
            expression: 数学表达式字符串，如 "2 + 3 * 4"

        Returns:
            计算结果字符串
        """
        result = self._safe_eval(expression)
        return str(result)

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "数学表达式，如 '2 + 3 * 4'"
                        }
                    },
                    "required": ["expression"]
                }
            }
        }

    def _safe_eval(self, expression: str) -> float:
        """
        安全地计算数学表达式

        使用 ast.literal_eval 的方式解析并计算表达式，
        只支持基本的数学运算符，避免安全问题。

        Args:
            expression: 数学表达式字符串

        Returns:
            float: 计算结果

        Raises:
            ValueError: 如果表达式无效
        """
        # 移除空白字符
        expression = expression.strip()

        # 使用 ast.parse 解析表达式
        node = ast.parse(expression, mode='eval')

        return self._eval_node(node.body)

    def _eval_node(self, node):
        """递归计算 AST 节点"""
        if isinstance(node, ast.Constant):
            # Python 3.8+ 使用 Constant 而不是 Num
            return node.value
        elif isinstance(node, ast.Num):
            return node.n
        elif isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            op_type = type(node.op)
            if op_type in self.OPERATORS:
                return self.OPERATORS[op_type](left, right)
            else:
                raise ValueError(f"不支持的运算符: {op_type.__name__}")
        elif isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            op_type = type(node.op)
            if op_type in self.OPERATORS:
                return self.OPERATORS[op_type](operand)
            else:
                raise ValueError(f"不支持的一元运算符: {op_type.__name__}")
        elif isinstance(node, ast.Expression):
            return self._eval_node(node.body)
        else:
            raise ValueError(f"不支持的表达式节点: {type(node).__name__}")