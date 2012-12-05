'''
Collection of algorithms for PyRate.
Author: Ben Davies, ANUSF
'''

from math import pi
from itertools import product

from numpy import sin, cos, radians, unique, histogram, std, mean
from numpy import float32, nan, isnan, sum as nsum, array, ndarray
from pygraph.classes.graph import graph
from pygraph.algorithms.minmax import minimal_spanning_tree

import config
from shared import EpochList


# constants
MM_PER_METRE = 1000


def wavelength_to_mm(data, wavelength):
	"""Converts ROIPAC phase from metres to millimetres"""
	return data * MM_PER_METRE * (wavelength / (4 * pi))


def los_conversion(phase_data, unit_vec_component):
	'''Converts phase from LOS to horizontal/vertical components. Args are
	numpy arrays.'''

	# NB: currently not tested as implementation is too simple
	return phase_data * unit_vec_component


def unit_vector(incidence, azimuth):
	vertical = cos(incidence)
	north_south = sin(incidence) * sin(azimuth)
	east_west = sin(incidence) * cos(azimuth)
	return east_west, north_south, vertical


def get_epochs(ifgs):
	masters = [i.MASTER for i in ifgs]
	slaves = [i.SLAVE for i in ifgs]

	combined = masters + slaves
	dates, n = unique(combined, False, True)
	repeat, _ = histogram(n, bins=len(set(n)))

	# absolute span for each date from the zero/start point
	span = [ (dates[i] - dates[0]).days / 365.25 for i in range(len(dates)) ]
	return EpochList(dates, repeat, span)


def ref_pixel(params, ifgs):
	'''Return (y,x) reference pixel coordinate given open Ifgs.'''

	head = ifgs[0]
	refx = params.get(config.REFX, 0)
	refy = params.get(config.REFY, 0)

	# sanity check any specified ref pixel settings
	if refx != 0 or refy != 0:
		if refx < 1 or refx > head.WIDTH - 1:
			raise ValueError("Invalid reference pixel X coordinate: %s" % refx)
		if refy < 1 or refy > head.FILE_LENGTH - 1:
			raise ValueError("Invalid reference pixel Y coordinate: %s" % refy)
		return (refy, refx)  # reuse preset ref pixel

	check_ref_pixel_params(params, head)

	# pre-calculate useful amounts
	refnx = params[config.REFNX]
	refny = params[config.REFNY]
	chipsize = params[config.REF_CHIP_SIZE]
	radius = chipsize / 2
	phase_stack = array([i.phase_data for i in ifgs]) # TODO: mem efficiencies?
	thresh = params[config.REF_MIN_FRAC] * chipsize * chipsize
	min_sd = float("inf") # dummy start value

	# do window searches across dataset, central pixel of stack with smallest mean
	# is the reference pixel
	for y in _step(head.FILE_LENGTH, refny, radius):
		for x in _step(head.WIDTH, refnx, radius):
			data = phase_stack[:, y-radius:y+radius+1, x-radius:x+radius+1]
			valid = [nsum(~isnan(i)) > thresh for i in data]

			if all(valid): # ignore stack if 1+ ifgs have too many incoherent cells
				sd = [std( i[~isnan(i)] ) for i in data]
				mean_sd = mean(sd)
				if mean_sd < min_sd:
					min_sd = mean_sd
					refy, refx = y, x

	if (refy, refx) == (0, 0):
		raise RefPixelError("Could not find a reference pixel")
	return refy, refx


def check_ref_pixel_params(params, head):
	'''Validates reference pixel search parameters. head is any Ifg.'''

	def missing_option_error(option):
		msg = "Missing '%s' in configuration options" % option
		raise config.ConfigException(msg)

	# sanity check chipsize setting
	chipsize = params.get(config.REF_CHIP_SIZE)
	if chipsize is None:
		missing_option_error(config.REF_CHIP_SIZE)

	if chipsize < 3 or chipsize > head.WIDTH or (chipsize % 2 == 0):
		raise ValueError("Chipsize setting must be >=3 and at least <= grid width")

	# sanity check minimum fraction
	min_frac = params.get(config.REF_MIN_FRAC)
	if min_frac is None:
		missing_option_error(config.REF_MIN_FRAC)

	if min_frac < 0.0 or min_frac > 1.0:
		raise ValueError("Minimum fraction setting must be >= 0.0 and <= 1.0 ")

	# sanity check X|Y steps
	refnx = params.get(config.REFNX)
	if refnx is None:
		missing_option_error(config.REFNX)

	max_width = (head.WIDTH - (chipsize-1))
	if refnx < 1 or refnx > max_width:
		raise ValueError("Invalid refnx setting, must be > 0 and < %s" % max_width)

	refny = params.get(config.REFNY)
	if refny is None:
		missing_option_error(config.REFNY)

	max_rows = (head.FILE_LENGTH - (chipsize-1))
	if refny < 1 or refny > max_rows:
		raise ValueError("Invalid refny setting, must be > 0 and < %s" % max_rows)


def _step(dim, ref, radius):
	'''Returns xrange obj of axis indicies for a search window. dim is the total
	length of the grid dimension. ref is the desired number of steps. radius is #
	cells out from the centre of the chip, or (chipsize / 2).'''

	if ref == 1:
		# centre a single search step
		return xrange(dim // 2, dim, dim) # fake step to ensure single xrange value

	max_dim = dim - (2*radius) # max possible number for refn(x|y)
	if ref == 2: # handle 2 search windows, method below doesn't cover the case
		return [radius, dim-radius-1]

	step = max_dim // (ref-1)
	return xrange(radius, dim, step)


def _remove_root_node(mst):
	"""Discard pygraph's root node from MST dict to conserve memory."""
	for k in mst.keys():
		if mst[k] is None:
			del mst[k]


def mst_matrix(ifgs, epochs):
	'''Returns array of minimum spanning trees for the Ifgs.'''

	# TODO: implement rows memory saving option/ row by row access?

	# locally cache all edges/weights for on-the-fly graph modification
	edges = [i.DATE12 for i in ifgs]
	weights = [i.nan_fraction for i in ifgs]

	# make default MST to optimise result when no Ifg cells in a stack are nans
	g = graph()
	g.add_nodes(epochs.dates) # each acquisition is a node
	for edge, weight in zip(edges, weights):
		g.add_edge(edge, wt=weight)

	default_mst = minimal_spanning_tree(g)
	_remove_root_node(default_mst)

	# prepare source and dest data arrays
	# [i.phase_data for i in ifgs]
	num_ifgs = len(ifgs)
	data_stack = array([i.phase_data for i in ifgs], dtype=float32)
	mst_result = ndarray(shape=(i.FILE_LENGTH, i.WIDTH), dtype=object)

	# create MSTs for each pixel in the ifg data stack
	for y, x in product(xrange(i.FILE_LENGTH), xrange(i.WIDTH)):
		values = data_stack[:,y,x] # select stack of all ifg values for a pixel
		nc = sum(isnan(values))

		# optimisations: use precreated results for all nans/no nans
		if nc == 0:
			mst_result[y,x] = default_mst
			continue
		elif nc == num_ifgs:
			mst_result[y,x] = nan
			continue

		# otherwise dynamically adjust graph, skipping edges where pixels are NaN
		for value, edge, weight in zip(values, edges, weights):
			if not isnan(value):
				if not g.has_edge(edge):
					g.add_edge(edge, wt=weight)
			else:
				if g.has_edge(edge):
					g.del_edge(edge)

		mst = minimal_spanning_tree(g)
		_remove_root_node(mst)
		mst_result[y,x] = mst

	return mst_result


class RefPixelError(Exception):
	pass