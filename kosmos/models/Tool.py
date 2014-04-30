from inspect import getargspec, getcallargs
import os
import re
from .. import TaskFile, Task
from ..util.helpers import parse_cmd, kosmos_format, groupby


opj = os.path.join


class ToolValidationError(Exception): pass


class _ToolMeta(type):
    def __init__(cls, name, bases, dct):
        cls.name = name
        return super(_ToolMeta, cls).__init__(name, bases, dct)


class Tool(object):
    """
    Essentially a factory that produces Tasks.  It's :meth:`cmd` must be overridden unless it is a NOOP task.
    """
    __metaclass__ = _ToolMeta

    mem_req = None
    time_req = None
    cpu_req = None
    must_succeed = None
    NOOP = False
    persist = False


    def __init__(self, tags, *args, **kwargs):
        """
        :param tags: (dict) A dictionary of tags.
        :param stage: (str) The stage this task belongs to.
        """

        #
        # #if len(tags)==0: raise ToolValidationError('Empty tag dictionary.  All tasks should have at least one tag.')

        if not hasattr(self, 'inputs'): self.inputs = []
        if not hasattr(self, 'outputs'): self.outputs = []
        if not hasattr(self, 'settings'): self.settings = {}
        if not hasattr(self, 'parameters'): self.parameters = {}
        if not hasattr(self, 'forward_inputs'): self.forward_inputs = []

        #TODO validate tags are strings and 1 level
        #self.tags = {k: str(v) for k, v in self.tags.items()}
        self.tags = tags

        self._validate()

    def map_inputs(self, parents):
        """
        Default method to map inputs.  Can be overriden if a different behavior is desired
        :returns: (list) a list of input taskfiles
        """
        if not self.inputs:
            return []

        else:
            if '*' in self.inputs:
                return {'*': [tf for p in parents for tf in p.all_outputs()]}

            all_inputs = filter(lambda x: x is not None,
                                [p.get_output(name, error_if_missing=False) for p in parents for name in
                                 self.inputs])

            input_names = [i.name for i in all_inputs]
            for k in self.inputs:
                if k not in input_names:
                    raise ValueError("Could not find input '{0}' for {1}".format(k, self))

            return all_inputs

    def generate_task(self, stage, parents, settings, parameters):
        d = {attr: getattr(self, attr) for attr in
             ['mem_req', 'time_req', 'cpu_req', 'must_succeed', 'NOOP']}
        input_files = self.map_inputs(parents)
        input_dict = {name: list(input_files) for name, input_files in groupby(input_files, lambda i: i.name)}
        task = Task(stage=stage, tags=self.tags, input_files=input_files, parents=parents,
                    forward_inputs=self.forward_inputs,
                    **d)

        # Create output TaskFiles
        output_files = []
        for output in self.outputs:
            if isinstance(output, tuple):
                if hasattr(output[1], '__call__'):
                    basename = output[1](i=input_dict, s=settings)
                else:
                    basename = output[1].format(i=input_dict, s=settings, **self.tags)
                tf = TaskFile(name=output[0], basename=basename,
                              task_output_for=task,
                              persist=self.persist)
            elif isinstance(output, str):
                tf = TaskFile(name=output, task_output_for=task, persist=self.persist)
            else:
                raise ToolValidationError("{0}.outputs must be a list of strs or tuples.".format(self))
            output_files.append(tf)
        if isinstance(self, Input):
            output_files.append(
                TaskFile(name=self.input_name, path=self.input_path, task_output_for=task, persist=True))
        elif isinstance(self, Inputs):
            for name, path in self.input_args:
                output_files.append(TaskFile(name=name, path=path, task_output_for=task, persist=True))

        task.tool = self
        self.settings = settings
        self.parameters = parameters

        return task

    def cmd(self, i, o, **kwargs):
        """
        Constructs the preformatted command string.  The string will be .format()ed with the i,s,p dictionaries,
        and later, $OUT.outname  will be replaced with a TaskFile associated with the output name `outname`

        :param i: (dict who's values are lists) Input TaskFiles.
        :param o: (dict) Output TaskFiles.
        :param kwargs: (dict) Parameters.  Received from, in order of precedence, tags, parameters and settings.
        :returns: (str|tuple(str,dict)) A pre-format()ed command string, or a tuple of the former and a dict with extra values to use for
            formatting
        """
        raise NotImplementedError("{0}.cmd is not implemented.".format(self.__class__.__name__))

    def generate_command(self, task):
        """
        Generates a command for a task.  Parameters are generated using, in order of precedence, tags, parameters, settings
        """
        argspec = getargspec(self.cmd)

        for k in self.parameters:
            if k not in argspec.args:
                raise ToolValidationError('Parameter %s is not a part of the %s.cmd signature' % (k, self))

        if {'inputs', 'outputs'}.issubset(argspec.args):
            signature_type = 'A'
        elif {'i', 'o'}.issubset(argspec.args):
            signature_type = 'B'
        else:
            raise ToolValidationError('Invalid %s.cmd signature'.format(self))

        # set parameters to settings
        p = {k: v for k, v in self.settings.items() if k in argspec.args}

        # update using parameters
        p.update(self.parameters)

        # update using tags
        p.update({k: v for k, v in task.tags.items() if k in argspec.args})

        for l in ['i', 'o', 'inputs', 'outputs']:
            if l in p.keys():
                raise ToolValidationError("%s is a reserved name, and cannot be used as a tag keyword" % l)

        try:
            input_dict = {name: list(input_files) for name, input_files in groupby(task.input_files, lambda i: i.name)}
            if signature_type == 'A':
                kwargs = dict(inputs=input_dict, outputs={o.name: o for o in task.output_files},
                              **p)
            elif signature_type == 'B':
                kwargs = dict(i=input_dict, o={o.name: o for o in task.output_files}, **p)
            callargs = getcallargs(self.cmd, **kwargs)
        except TypeError:
            raise TypeError('Invalid parameters for {0}.cmd(): {1}'.format(self, kwargs.keys()))

        del callargs['self']
        r = self.cmd(**callargs)

        #if tuple is returned, second element is a dict to format with
        extra_format_dict = r[1] if len(r) == 2 and r else {}
        pcmd = r[0] if len(r) == 2 else r

        #format() return string with callargs
        callargs['self'] = self
        callargs['task'] = task
        callargs.update(extra_format_dict)
        cmd = kosmos_format(pcmd, callargs)

        #fix TaskFiles paths
        cmd = re.sub('<TaskFile\[\d+?\] .+?:(.+?)>', lambda x: x.group(1), cmd)

        return parse_cmd(cmd)


    def _validate(self):
        #validate inputs are strs
        if any([not isinstance(i, str) for i in self.inputs]):
            raise ToolValidationError, "{0} has elements in self.inputs that are not of type str".format(self)

        if len(self.inputs) != len(set(self.inputs)):
            raise ToolValidationError(
                'Duplicate names in task.inputs detected in {0}.  Perhaps try using [1.ext,2.ext,...]'.format(self))

        if len(self.outputs) != len(set(self.outputs)):
            raise ToolValidationError(
                'Duplicate names in task.taskfiles detected in {0}.'
                '  Perhaps try using [1.ext,2.ext,...] when defining outputs'.format(self))


class Input(Tool):
    """
    A NOOP Task who's output_files contain a *single* file that already exists on the filesystem.

    Does not actually execute anything, but provides a way to load an input file.

    >>> Input('ext','/path/to/file.ext',tags={'key':'val'})
    >>> Input(path='/path/to/file.ext.gz',name='ext',tags={'key':'val'})
    """

    name = 'Load_Input_Files'

    def __init__(self, name, path, tags, *args, **kwargs):
        """
        :param path: the path to the input file
        :param name: the name or keyword for the input file
        :param fmt: the format of the input file
        """
        path = os.path.abspath(path)
        assert os.path.exists(path), '%s path does not exist for %s' % (path, self)
        super(Input, self).__init__(tags=tags, *args, **kwargs)
        self.NOOP = True
        # if name is None:
        #     _, name = os.path.splitext(path)
        #     name = name[1:] # remove '.'
        #     assert name != '', 'name not specified, and path has no extension'

        self.input_path = path
        self.input_name = name


class Inputs(Tool):
    """
    An Input File.A NOOP Task who's output_files contain a *multiple* files that already exists on the filesystem.

    Does not actually execute anything, but provides a way to load a set of input file.

    >>> Input('ext','/path/to/file.ext',tags={'key':'val'})
    >>> Input(path='/path/to/file.ext.gz',name='ext',tags={'key':'val'})
    """
    name = 'Load_Input_Files'

    def __init__(self, inputs, tags=None, *args, **kwargs):
        """
        :param path: the path to the input file
        :param name: the name or keyword for the input file
        :param fmt: the format of the input file
        """
        if tags is None:
            tags = dict()
            #path = os.path.abspath(path)
        super(Inputs, self).__init__(tags=tags, *args, **kwargs)
        self.NOOP = True

        def abs(path):
            path2 = os.path.abspath(path)
            assert os.path.exists(path2), '%s path does not exist for %s' % (path2, self)
            return path2

        inputs = [(name, abs(path)) for name, path in inputs]
        self.input_args = inputs