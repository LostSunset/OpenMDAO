"""
Tools for working with code.
"""

import sys
import os
import inspect
import ast
import textwrap
import importlib
from types import LambdaType
from collections import defaultdict, OrderedDict

import networkx as nx


def _get_long_name(node):
    # If the node is an Attribute or Name node that is composed
    # only of other Attribute or Name nodes, then return the full
    # dotted name for this node. Otherwise, i.e., if this node
    # contains Subscripts or Calls, return None.
    if isinstance(node, ast.Name):
        return node.id
    elif not isinstance(node, ast.Attribute):
        return None
    val = node.value
    parts = [node.attr]
    while True:
        if isinstance(val, ast.Attribute):
            parts.append(val.attr)
            val = val.value
        elif isinstance(val, ast.Name):
            parts.append(val.id)
            break
        else:  # it's more than just a simple dotted name
            return None
    return '.'.join(parts[::-1])


class _SelfCallCollector(ast.NodeVisitor):
    """
    An ast.NodeVisitor that records calls to self.* methods.
    """

    def __init__(self, class_):
        super().__init__()
        self.self_calls = defaultdict(list)
        self.class_ = class_
        self.mro = inspect.getmro(class_)
        self.mro_names = set([c.__name__ for c in self.mro])

    def visit_Call(self, node):  # (func, args, keywords, starargs, kwargs)
        fncname = _get_long_name(node.func)
        class_ = self.class_
        if fncname is not None:
            if fncname.startswith('self.') and len(fncname.split('.')) == 2:
                shortfnc = fncname.split('.')[1]
                if shortfnc not in self.self_calls[class_]:
                    self.self_calls[class_].append(shortfnc)
                for arg in node.args:
                    self.visit(arg)
            # check for Class.func(inst) form for base class method call
            elif (len(fncname.split('.')) == 2 and fncname.split('.')[0] in self.mro_names and
                  node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == 'self'):
                cname, func = fncname.split('.')
                for c in self.mro:
                    if c.__name__ == cname:
                        sub_mro = inspect.getmro(c)
                        for sub_c in sub_mro:
                            if func in sub_c.__dict__:
                                c = sub_c
                                break
                        if func not in self.self_calls[c]:
                            self.self_calls[c].append(func)
                        for arg in node.args:
                            self.visit(arg)
                        break
                else:
                    self.generic_visit(node)
            else:
                self.generic_visit(node)
        # check for super() call
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
            callnode = node.func.value
            n = _get_long_name(callnode.func)
            # if this is a 'super' call, get the base of the specified class
            if n == 'super':  # this only works for a single call level
                if len(callnode.args) == 0:
                    sup_0 = self.mro[0].__name__
                    visit_super = True
                else:
                    sup_1 = _get_long_name(callnode.args[1])
                    sup_0 = _get_long_name(callnode.args[0])
                    visit_super = (sup_1 == 'self' and
                                   sup_0 is not None and len(sup_0.split('.')) == 1)

                if visit_super:
                    for i, c in enumerate(self.mro[:-1]):
                        if sup_0 == c.__name__:
                            # we need super of the specified class
                            sub_mro = inspect.getmro(c)
                            for sub_c in sub_mro:
                                if sub_c is not c:
                                    c = sub_c
                                    break
                            fn = node.func.attr
                            if fn not in self.self_calls[c]:
                                self.self_calls[c].append(fn)
                            for arg in node.args:
                                self.visit(arg)
                            break
                    else:
                        self.generic_visit(node)
            else:
                self.generic_visit(node)
        else:
            self.generic_visit(node)


def _find_owning_class(mro, func_name):
    """
    Return the full funcname and class where the function is first found in the class MRO.
    """
    # TODO: this won't work for classes with __slots__

    for c in mro:
        if func_name in c.__dict__:
            return '.'.join((c.__name__, func_name)), c

    return None, None


def _get_nested_calls(starting_class, class_, func_name, parent, graph, seen):
    """
    Parse the AST of the given method and all 'self' methods it calls and record owning classes.
    """
    func = getattr(class_, func_name)
    src = inspect.getsource(func)
    dedented_src = textwrap.dedent(src)

    node = ast.parse(dedented_src, mode='exec')
    visitor = _SelfCallCollector(starting_class)
    visitor.visit(node)

    seen.add('.'.join((class_.__name__, func_name)))

    # now find the actual owning class for each call
    for klass, funcset in visitor.self_calls.items():
        mro = inspect.getmro(klass)
        for f in funcset:
            full, c = _find_owning_class(mro, f)
            if full is not None:
                graph.add_edge(parent, full)
                if full not in seen:
                    _get_nested_calls(starting_class, c, f, full, graph, seen)


def get_nested_calls(class_, method_name, stream=sys.stdout):
    """
    Display the call tree for the specified class method and all 'self' class methods it calls.

    Parameters
    ----------
    class_ : class
        The starting class.
    method_name : str
        The name of the class method.
    stream : file-like
        The output stream where output will be displayed.

    Returns
    -------
    networkx.DiGraph
        A graph containing edges from methods to their sub-methods.
    """
    # moved this class def in here to keep the numpy doc scraper from barfing due to
    # stuff in nx.DiGraph.
    class OrderedDiGraph(nx.DiGraph):
        """
        A DiGraph using OrderedDicts for internal storage.
        """

        node_dict_factory = OrderedDict
        adjlist_dict_factory = OrderedDict
        edge_attr_dict_factory = OrderedDict

    graph = OrderedDiGraph()
    seen = set()
    top = object()

    full, klass = _find_owning_class(inspect.getmro(class_), method_name)
    if full is None:
        print("Can't find function '%s' in class '%s'." % (method_name, class_.__name__))
    else:
        graph.add_edge(top, full)
        parent = full
        _get_nested_calls(class_, klass, method_name, parent, graph, seen)

    if graph and stream is not None:
        seen = set([top])
        stack = [(0, iter(graph[top]))]
        while stack:
            depth, children = stack[-1]
            try:
                n = next(children)
                stream.write("%s%s\n" % ('  ' * depth, n))
                if n not in seen:
                    stack.append((depth + 1, iter(graph[n])))
                    seen.add(n)
            except StopIteration:
                stack.pop()

    return graph


def _calltree_setup_parser(parser):
    """
    Set up the command line options for the 'openmdao call_tree' command line tool.
    """
    parser.add_argument('method_path', nargs=1,
                        help='Full module path to desired class method, e.g., '
                        '"openmdao.components.exec_comp.ExecComp.setup".')
    parser.add_argument('-o', '--outfile', action='store', dest='outfile',
                        default='stdout', help='Output file.  Defaults to stdout.')


def _calltree_exec(options, user_args):
    """
    Process command line args and call get_nested_calls on the specified class method.
    """
    parts = options.method_path[0].split('.')
    if len(parts) < 3:
        raise RuntimeError("You must supply the full module path to the function, "
                           "for example:  openmdao.api.Group._setup.")

    class_name = parts[-2]
    func_name = parts[-1]
    modpath = '.'.join(parts[:-2])

    old_syspath = sys.path[:]
    sys.path.append(os.getcwd())

    try:
        mod = importlib.import_module(modpath)
        klass = getattr(mod, class_name)

        stream_map = {'stdout': sys.stdout, 'stderr': sys.stderr}
        stream = stream_map.get(options.outfile)
        if stream is None:
            stream = open(options.outfile, 'w')

        get_nested_calls(klass, func_name, stream)
    finally:
        sys.path = old_syspath


def _target_iter(targets):
    for target in targets:
        if isinstance(target, ast.Tuple):
            for t in target.elts:
                yield t
        else:
            yield target


class _AttrCollector(ast.NodeVisitor):
    """
    An ast.NodeVisitor that records class attribute names.
    """

    def __init__(self, class_dict):
        super().__init__()
        self.class_dict = class_dict
        self.class_stack = []
        self.func_stack = []
        self.names = None
        self.decnames = None

    def get_attributes(self):
        return self.class_dict

    def visit_ClassDef(self, node):
        full_name = '.'.join(self.class_stack[:] + [node.name])
        self.class_stack.append(full_name)
        self.class_dict[full_name] = set()
        for stmt in node.body:
            self.visit(stmt)
        self.class_stack.pop()

        if self.func_stack:  # ignore classes nested in functs
            del self.class_dict[full_name]

    def visit_FunctionDef(self, node):
        self.func_stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.func_stack.pop()

        if self.class_stack:
            # see if this is a property, and if so, treat as an attribute
            for dec in node.decorator_list:
                self.decnames = []
                self.visit(dec)
                if len(self.decnames) == 1 and self.decnames[0] == 'property':
                    self.class_dict[self.class_stack[-1]].add(node.name)

        self.decnames = None

    def visit_Assign(self, node):
        if self.class_stack:
            for t in _target_iter(node.targets):
                self.names = []
                self.visit(t)
                if len(self.names) > 1 and self.names[0] == 'self':
                    self.class_dict[self.class_stack[-1]].add(self.names[1])

            self.names = None

    def visit_Attribute(self, node):
        if self.names is not None:
            self.visit(node.value)
            self.names.append(node.attr)

    def visit_Name(self, node):
        if self.names is not None:
            self.names.append(node.id)
        elif self.decnames is not None:
            self.decnames.append(node.id)


def get_class_attributes(fname, class_dict=None):
    """
    Find all referenced attributes in all classes defined in the given file.

    Parameters
    ----------
    fname : str
        File name.
    class_dict : dict or None
        Dict mapping class names to attribute names.

    Returns
    -------
    dict
        The dict maps class name to a set of attribute names.
    """
    if class_dict is None:
        class_dict = {}

    with open(fname, 'r') as f:
        source = f.read()
        node = ast.parse(source, mode='exec')
        visitor = _AttrCollector(class_dict)
        visitor.visit(node)
        return visitor.get_attributes()


def is_lambda(f):
    """
    Return True if the given function is a lambda function.

    Parameters
    ----------
    f : function
        The function to check.

    Returns
    -------
    bool
        True if the given function is a lambda function.
    """
    return isinstance(f, LambdaType) and f.__name__ == "<lambda>"


class LambdaPickleWrapper(object):
    """
    A wrapper for a lambda function that allows it to be pickled.

    Parameters
    ----------
    lambda_func : function
        The lambda function to be wrapped.

    Attributes
    ----------
    _func : function
        The lambda function.
    _src : str
        The isolated source of the lambda function.
    """

    def __init__(self, lambda_func):
        """
        Initialize the wrapper.

        Parameters
        ----------
        lambda_func : function
            The lambda function to be wrapped.
        """
        self._func = lambda_func
        self._src = None

    def __call__(self, *args, **kwargs):
        """
        Call the lambda function.

        Parameters
        ----------
        *args : list
            Positional arguments.
        **kwargs : dict
            Keyword arguments.

        Returns
        -------
        object
            The result of the lambda function.
        """
        return self._func(*args, **kwargs)

    def __getstate__(self):
        """
        Return the state of this object for pickling.

        The lambda function is converted to a string for pickling.

        Returns
        -------
        dict
            The state of this object.
        """
        state = self.__dict__.copy()
        state['_func'] = self._getsrc()
        return state

    def __setstate__(self, state):
        """
        Restore the state of this object after pickling.

        Parameters
        ----------
        state : dict
            The state of this object.
        """
        self.__dict__.update(state)
        self._func = eval(state['_func'])  # nosec

    def _getsrc(self):
        if self._src is None:
            self._src = _LambdaSrcFinder(self._func).src
            if self._src is None:
                raise RuntimeError("The fix for pickling lambda functions only works for python "
                                   "3.9 and above. Try updating to a newer python version or "
                                   "replacing the lambda with a regular function.")
        return self._src


class _LambdaSrcFinder(ast.NodeVisitor):
    """
    Given a lambda function, isolate the lambda function source from any surrounding code.
    """

    def __init__(self, func):
        super().__init__()
        self.src = None
        # note that inspect.getsource gives the source for the line that contains the lambda
        # function, so we have to isolate the lambda function itself
        self.visit(ast.parse(textwrap.dedent(inspect.getsource(func)), filename='<string>'))

    def visit_Lambda(self, node):
        if self.src is not None:
            # it's possible to have multiple lambdas defined on the same line, so raise an error
            # if we find more than one.
            raise RuntimeError("Only one lambda function is allowed per line when using "
                               "_LambdaSrcFinder.")
        try:
            self.src = ast.unparse(node)
        except AttributeError:
            # ast.unparse was added in python 3.9
            self.src = None


if __name__ == '__main__':
    import pprint
    pprint.pprint(get_class_attributes(__file__))
