"""
This is turning into the API submodule for
"""
from typing import Callable, Iterable, Dict, Tuple, FrozenSet, Set, NamedTuple
import operator
from boozetools.support import foundation
from . import frontend, runtime, domain, semantics

__ALL__ = ['Universe', 'AlreadyRegistered']

class AlreadyRegistered(Exception):
	""" The key for some sort of registration API call has already been used in a previous call. """

class UsageConflict(Exception):
	""" The same-named query variable is used in conflicting ways. """

class MistakeModule:
	"""
	Think of this as like a schema for a database. A application has
		1. a universe of discourse
		2. a registry of `TransformFunction` objects,
		3. a registry of `AbstractTensor` objects.
	"""
	
	__transforms: Dict[Tuple[FrozenSet, FrozenSet], domain.Transform]
	__tensors: Dict[str, domain.AbstractTensor]
	__variables: Dict[str, Tuple[str, bool]] # from variable name to (axis, plural)
	__units: Set[str]
	
	def __init__(self, universe:semantics.UniverseOfDiscourse):
		assert isinstance(universe, semantics.UniverseOfDiscourse), type(universe)
		self.__universe = universe
		self.__transforms = {}
		self.__tensors = {}
		self.__variables = {}
	
	def register_transform(self, transform:domain.Transform):
		def validate_space(space):
			for d in space:
				if not (isinstance(d, str)): raise TypeError('should have been string', type(d))
				if d != d.lower(): raise ValueError('should have been lower-case', d)
				try: item = self.__universe[d]
				except KeyError: raise ValueError('dimension %r is not known.'%d)
				if not isinstance(item, semantics.Axis): raise ValueError('%r is a(n) %r, not an axis.'%(d, type(item)))
		
		validate_space(transform.domain)
		validate_space(transform.range)
		assert transform.range
		key = (frozenset(transform.domain), frozenset(transform.range)) ## Defensive programming? Meh.
		if key in self.__transforms: raise AlreadyRegistered(key)
		else: self.__transforms[key] = transform
	
	def register_attribute(self, domain_:str, range_:str, function):
		# This should really be an aspect of a dimension.
		# Attribute access (or mapping) should probably have corresponding grammar.
		def update(p): p[range_] = function(p[domain_])
		transform = domain.Transform(frozenset((domain_,)), frozenset((range_,)), update)
		self.register_transform(transform)
	
	def register_tensor(self, name:str, tensor:domain.AbstractTensor):
		if name != name.lower(): raise ValueError(name)
		if name in self.__tensors: raise AlreadyRegistered(name)
		assert isinstance(tensor, domain.AbstractTensor), type(tensor)
		self.__tensors[name] = tensor
	
	def cast_variable(self, name:str, axis:str, plural:bool):
		if name not in self.__variables: self.__variables[name] = (axis, plural)
		elif self.__variables[name] != (axis, plural): raise UsageConflict(name)
	
	def tensor_types(self) -> Dict[str, domain.Space]:
		return {name: tensor.tensor_type() for name, tensor in self.__tensors.items()}

	def find_transform(self, domain_:Iterable[str], range_:Iterable[str]):
		key = (frozenset(domain_), frozenset(range_))
		return self.__transforms.get(key)
	
	def get_tensor(self, name:str):
		return self.__tensors[name]
	
	def query(self, name:str, /, **kwargs):
		tensor = self.get_tensor(name.lower())
		# TODO: Validate that the correct environment variables are provided,
		#  and with a value acceptable to the variable's inferred dimension.
		return runtime.TensorBuffer(tensor, domain.Predicate([]), kwargs)

	def script(self, text:str):
		""" Call this to parse and load a script full of definitions. """
		parser = frontend.Parser()
		ast = parser.parse(text)
		if ast is not None:
			Planner(self, parser.source.complain).visit(ast)
		return self

class Gripe(Exception):
	""" The Planner raises this with target-language source diagnostic data. """
	def __init__(self, span:Tuple[int, int], message:str): self.span, self.message = span, message
	def gripe(self, how): how(*self.span, message=self.message)

class BTOS(NamedTuple):
	combine_space:Callable
	combine_units:Callable
	construct_plan:Callable

STRATEGY = {
	"+": BTOS(semantics.require_spatial_symmetry, operator.add, runtime.SumTensor),
	"-": BTOS(semantics.require_spatial_symmetry, operator.sub, runtime.difference),
	"*": BTOS(semantics.require_spatial_symmetry, operator.mul, runtime.Product),
	"/": BTOS(semantics.require_spatial_symmetry, operator.truediv, runtime.Quotient),
}

class Planner(foundation.Visitor):
	"""
	This class integrates semantic validation with query planning.
	It converts AST nodes (from the parser) into a suitable run-time
	structure concurrent with static analysis to determine whether
	the elements of each definition have sound semantics -- and if
	otherwise, then localizing the inconsistencies for easy
	diagnosis and repair.
	"""
	def __init__(self, universe:MistakeModule, complain:Callable, verbose=False):
		self.__universe = universe
		self.__complain = complain
		self.__verbose = verbose
		self.__type_env = universe.tensor_types()
		
	def visit_list(self, items):
		for i in items: self.visit(i)
	
	def visit_DefineTensor(self, dt:frontend.DefineTensor):
		if dt.name.text in self.__type_env:
			self.__complain(*dt.name.span, message="Name was previously defined; ignoring redefinition.")
		else:
			try: tensor = self.visit(dt.expr)
			except Gripe as e:
				self.__complain(*dt.name.span, message="Tensor variable has invalid type, because...")
				e.gripe(self.__complain)
				tt = None
			else:
				self.__universe.register_tensor(dt.name.text, tensor) # Contains type assertion.
				tt = tensor.tensor_type()
				if self.__verbose: print("%s has shape %r and units of '%s'"%(dt.name.text, sorted(tt.space), tt.unit))
			self.__type_env[dt.name.text] = tt
	
	def visit_BinaryTensorOp(self, d: frontend.BinaryTensorOp):
		strategy = STRATEGY[d.symbol]
		lhs, rhs = self.visit(d.a_exp), self.visit(d.b_exp)
		lt, rt = lhs.tensor_type(), rhs.tensor_type()
		try:
			space = strategy.combine_space(lt.space, rt.space)
			unit = strategy.combine_units(lt.unit, rt.unit)
		except semantics.Invalid as e:
			raise Gripe(d.span, e.message)
		return strategy.construct_plan(lhs, rhs, semantics.TensorType(space, unit))
	
	def visit_Multiplex(self, m:frontend.Multiplex):
		if_true, if_false = self.visit(m.if_true), self.visit(m.if_false)
		if if_true.tensor_type() != if_false.tensor_type():
			raise Gripe(m.keyword_span, "Left and right side tensors must have identical type")
		return runtime.Multiplex(if_true, self.visit(m.criterion, if_true.tensor_type()), if_false)
	
	def visit_Filter(self, f:frontend.Filter):
		basis = self.visit(f.basis)
		return runtime.Filter(basis, self.visit(f.criterion, basis.tensor_type()))
	
	def visit_ScalarComparison(self, c:frontend.ScalarComparison, tt:semantics.TensorType) -> runtime.ScalarComparison:
		assert isinstance(tt, semantics.TensorType), type(tt)
		if c.axis.text not in tt.space: _unavailable(c.axis, tt.space)
		if isinstance(c.rhs, frontend.Name):
			try: self.__universe.cast_variable(c.rhs.text, c.axis.text, False)
			except UsageConflict: _conflict(c.rhs)
			scalar = runtime.Variable(c.rhs.text)
		elif isinstance(c.rhs, (str,int,float)):
			# TODO: Compare the argument and the relation to the type of the dimension.
			scalar = runtime.Constant(c.rhs)
		else: assert False, type(c.rhs)
		return runtime.ScalarComparison(c.axis.text, c.relop, scalar)
	
	def visit_Aggregation(self, agg:frontend.Aggregation):
		basis = self.visit(agg.a_exp)
		if isinstance(basis, Gripe): return basis
		assert isinstance(basis, domain.AbstractTensor), [type(agg.a_exp), type(basis)]
		new_space = set()
		for dim in agg.new_space:
			if dim.text in new_space: _already(dim)
			if dim.text not in basis.tensor_type().space: _unavailable(dim, basis.tensor_type().space)
			new_space.add(dim.text)
		# TODO: At this point, in theory we could have an invalid aggregation. However, I don't have the means to check yet.
		#  To clarify: Either a dimension might not be safe to sum over (yet we did) or a remaining dimension may
		#  be strictly subordinate to one we DID sum over.
		return runtime.Aggregation(basis, new_space)
	
	def visit_Name(self, n:frontend.Name) -> domain.AbstractTensor:
		try: return self.__universe.get_tensor(n.text)
		except KeyError:
			if n.text in self.__type_env: raise Gripe(n.span, "ill-typed name.")
			else: raise Gripe(n.span, "undefined name.")
	
	def visit_ScaleBy(self, s:frontend.ScaleBy):
		return self.visit(s.a_exp)

	def visit_SumImage(self, si:frontend.SumImage) -> runtime.Transformation:
		# Ideally you'd like to compute the transforms in parallel to avoid
		# making multiple passes through the data.
		basis = self.visit(si.a_exp)
		assert isinstance(basis, domain.AbstractTensor), type(basis)
		effective_space = set(basis.tensor_type().space)
		procedure = None
		for mx in si.sums:
			# First, apply a simple textual test:
			for dim in mx.domain:
				try: effective_space.remove(dim.text)
				except KeyError: _unavailable(dim, effective_space)
			for dim in mx.range:
				if dim.text in effective_space: _already(dim)
				else: effective_space.add(dim.text)
			# Next, do we have a registered function in the "universe" to perform this translation?
			def texts(names): return [n.text for n in names]
			step = self.__universe.find_transform(texts(mx.domain), texts(mx.range))
			if step is None: raise Gripe(mx.op_span, "No known transform applies.")
			procedure = _sequence(procedure, step)
		# Last step: Consider any explicit aggregation. (See also visit_Aggregation.)
		if si.space is not None:
			new_space = set()
			for dim in si.space:
				if dim.text in new_space: _already(dim)
				if dim.text not in effective_space: _unavailable(dim, effective_space)
				new_space.add(dim.text)
			effective_space = new_space
		return runtime.Transformation(basis, frozenset(effective_space), procedure)

def _unavailable(dim:frontend.Name, ops:Iterable[str]):
	raise Gripe(dim.span, "Dimension %r is not available here. options are %r." % (dim.text, sorted(ops)))

def _already(dim:frontend.Name):
	raise Gripe(dim.span, "Dimension %r is already present and may not be duplicated." % dim.text)

def _sequence(procedure, step):
	if procedure is None: return step
	def two_step(p): procedure(p); step(p)
	return two_step

def _conflict(var:frontend.Name):
	raise Gripe(var.span, "Variable %r is used earlier in an incompatible manner. (It must agree in dimension and grammatical number.)" % var.text)
