# Authors:
#     Loic Gouarin <loic.gouarin@polytechnique.edu>
#     Benjamin Graille <benjamin.graille@math.u-psud.fr>
#
# License: BSD 3 clause
"""
Domain definitions for LBM
"""
import logging
import sys
import copy

import numpy as np
import sympy as sp
import mpi4py.MPI as mpi
from six.moves import range
from six import string_types

from .geometry import Geometry
from .stencil import Stencil
from .mpi_topology import MpiTopology
from .validator import validate
from . import viewer

log = logging.getLogger(__name__) #pylint: disable=invalid-name

class Domain:
    """
    Create a domain that defines the fluid part and the solid part
    and computes the distances between these two states.

    Parameters
    ----------

    dico : a dictionary that contains the following `key:value`

        - box : a dictionary that defines the computational box
        - elements : the list of the elements
          (available elements are given in the module :py:mod:`elements <pylbm.elements>`)
        - space_step : the spatial step
        - schemes : a list of dictionaries,
          each of them defining a elementary :py:class:`Scheme <pylbm.scheme.Scheme>`

    Notes
    -----

    The dictionary that defines the box should contains the following `key:value`

    - x : a list of the bounds in the first direction
    - y : a list of the bounds in the second direction (optional)
    - z : a list of the bounds in the third direction (optional)
    - label : an integer or a list of integers
      (length twice the number of dimensions)
      used to label each edge (optional)

    See :py:class:`Geometry <pylbm.geometry.Geometry>` for more details.

    If the geometry and/or the stencil were previously generated,
    it can be used directly as following

    >>> Domain(dico, geometry = geom, stencil = sten)

    where geom is an object of the class
    :py:class:`Geometry <pylbm.geometry.Geometry>`
    and sten an object of the class
    :py:class:`Stencil <pylbm.stencil.Stencil>`
    In that case, dico does not need to contain the informations for generate
    the geometry and/or the stencil

    In 1D, distance[q, i] is the distance between the point x[i]
    and the border in the direction of the qth velocity.

    In 2D, distance[q, j, i] is the distance between the point
    (x[i], y[j]) and the border in the direction of qth
    velocity

    In 3D, distance[q, k, j, i] is the distance between the point
    (x[i], y[j], z[k]) and the border in the direction of qth
    velocity

    In 1D, flag[q, i] is the flag of the border reached by the point
    x[i] in the direction of the qth velocity

    In 2D, flag[q, j, i] is the flag of the border reached by the point
    (x[i], y[j]) in the direction of qth velocity

    In 2D, flag[q, k, j, i] is the flag of the border reached by the point
    (x[i], y[j], z[k]) in the direction of qth velocity

    Warnings
    --------

    the sizes of the box must be a multiple of the space step dx

    Attributes
    ----------

    dim : int
      number of spatial dimensions (example: 1, 2, or 3)
    globalbounds : ndarray
      the bounds of the box in each spatial direction
    bounds : ndarray
      the local bounds of the process in each spatial direction
    dx : double
      space step (example: 0.1, 1.e-3)
    type : string
      type of data (example: 'float64')
    stencil : Stencil
      the stencil of the velocities (object of the class
      :py:class:`Stencil <pylbm.stencil.Stencil>`)
    global_size : list
      number of points in each direction
    extent : list
      number of points to add on each side (max velocities)
    coords : ndarray
      coordinates of the domain
    x : ndarray
      first coordinate of the domain
    y : ndarray
      second coordinate of the domain (None if dim<2)
    z : ndarray
      third coordinate of the domain (None if dim<3)
    in_or_out : ndarray
      defines the fluid and the solid part
      (fluid: value=valin, solid: value=valout)
    distance : ndarray
      defines the distances to the borders.
      The distance is scaled by dx and is not equal to valin only for
      the points that reach the border with the specified velocity.
    flag : ndarray
      NumPy array that defines the flag of the border reached with the
      specified velocity
    valin : int
        value in the fluid domain
    valout : int
        value in the fluid domain
    x_halo : ndarray
        first coordinate of the domain with the overlap
    y_halo : ndarray
        second coordinate of the domain with the overlap
    z_halo : ndarray
        third coordinate of the domain with the overlap
    shape_halo : list
        the shape of the domain with overlap
    shape_in
        the shape of the domain without overlap

    Examples
    --------

    see demo/examples/domain/


    """

    def __init__(self, dico=None, geometry=None, stencil=None, space_step=None, need_validation=True):
        self.valin = 999  # value in the fluid domain
        self.valout = -1   # value in the solid domain

        if dico is not None and need_validation:
            validate(dico, __class__.__name__)

        self.geom = Geometry(dico, need_validation=False) if geometry is None else geometry
        self.stencil = Stencil(dico, need_validation=False) if stencil is None else stencil
        self.dx = dico['space_step'] if space_step is None else space_step
        self.dim = self.geom.dim

        self.box_label = copy.copy(self.geom.box_label)

        self.mpi_topo = None
        self.construct_mpi_topology(dico)

        self.global_size = []
        self.create_coords()

        region = self.mpi_topo.get_region(*self.global_size) #pylint: disable=no-value-for-parameter

        # Modify box_label if the border becomes an interface
        for i in range(self.dim):
            if region[i][0] != 0:
                self.box_label[2*i] = -2
            if region[i][1] != self.global_size[i]:
                self.box_label[2*i + 1] = -2

        # distance to the borders
        total_size = [self.stencil.unvtot] + self.shape_halo
        self.in_or_out = self.valin*np.ones(self.shape_halo)
        self.distance = self.valin*np.ones(total_size)
        self.flag = self.valin*np.ones(total_size, dtype='int')

        self.__add_init(self.box_label) # compute the distance and the flag for the primary box
        for elem in self.geom.list_elem: # treat each element of the geometry
            self.__add_elem(elem)

        log.info(self.__str__())

    @property
    def shape_halo(self):
        """
        shape of the whole domain with the halo points.
        """
        return [c.size for c in self.coords_halo]

    @property
    def shape_in(self):
        """
        shape of the interior domain.
        """
        return [c.size for c in self.coords]

    @property
    def x(self):
        """
        x component of the coordinates in the interior domain.
        """
        return self.coords[0]

    @property
    def y(self):
        """
        y component of the coordinates in the interior domain.
        """
        return self.coords[1]

    @property
    def z(self):
        """
        z component of the coordinates in the interior domain.
        """
        return self.coords[2]

    @property
    def x_halo(self):
        """
        x component of the coordinates of the whole domain (halo points included).
        """
        return self.coords_halo[0]

    @property
    def y_halo(self):
        """
        y component of the coordinates of the whole domain (halo points included).
        """
        return self.coords_halo[1]

    @property
    def z_halo(self):
        """
        z component of the coordinates of the whole domain (halo points included).
        """
        return self.coords_halo[2]

    def __str__(self):
        s = "Domain informations\n"
        s += "\t spatial dimension: {0:d}\n".format(self.dim)
        #s += "\t bounds of the box: bounds = " + self.bounds.__str__() + "\n"
        s += "\t space step: dx={0:10.3e}\n".format(self.dx)
        #s += "\t Number of points in each direction: N=" + self.N.__str__() + ", Na=" + self.Na.__str__() + "\n"
        return s

    def construct_mpi_topology(self, dico):
        """
        Create the mpi topology
        """
        period = [True]*self.dim

        if dico is None:
            comm = mpi.COMM_WORLD
        else:
            comm = dico.get('comm', mpi.COMM_WORLD)
        self.mpi_topo = MpiTopology(self.dim, period, comm)

    def create_coords(self):
        """
        Create the coordinates of the interior domain and the whole domain with halo points.
        """
        phys_box = self.geom.bounds # the physical box where the domain lies

        # validation of the space step with the physical box size
        for k in range(self.dim):
            self.global_size.append((phys_box[k][1] - phys_box[k][0])/self.dx)
            if not self.global_size[-1].is_integer():
                log.error('The length of the box in the direction %d must be a multiple of the space step', k)
                sys.exit()

        region = self.mpi_topo.get_region(*self.global_size) #pylint: disable=no-value-for-parameter
        region_size = [r[1] - r[0] for r in region]

        # spatial mesh
        halo_size = np.asarray(self.stencil.vmax)
        halo_beg = self.dx*(halo_size - 0.5)

        self.coords_halo = [np.linspace(phys_box[k][0] + self.dx*region[k][0] - halo_beg[k],
                                        phys_box[k][0] + self.dx*region[k][1] + halo_beg[k],
                                        region_size[k] + 2*halo_size[k]) for k in range(self.dim)]

        self.coords = [self.coords_halo[k][halo_size[k]:-halo_size[k]] for k in range(self.dim)]

    def get_bounds_halo(self):
        """
        Return the coordinates of the bottom right and upper left corner of the
        whole domain with halo points.
        """
        bottom_right = np.asarray([self.coords_halo[k][0] for k in range(self.dim)])
        upper_left = np.asarray([self.coords_halo[k][-1] for k in range(self.dim)])
        return bottom_right, upper_left

    def get_bounds(self):
        """
        Return the coordinates of the bottom right and upper left corner of the
        interior domain.
        """
        bottom_right = np.asarray([self.coords[k][0] for k in range(self.dim)])
        upper_left = np.asarray([self.coords[k][-1] for k in range(self.dim)])
        return bottom_right, upper_left

    #pylint: disable=too-many-locals
    def __add_init(self, label):
        halo_size = np.asarray(self.stencil.vmax)
        phys_domain = [slice(h, -h) for h in halo_size]

        self.in_or_out[:] = self.valout

        in_view = self.in_or_out[tuple(phys_domain)]
        in_view[:] = self.valin

        phys_domain.insert(0, slice(None))
        dist_view = self.distance[tuple(phys_domain)]
        flag_view = self.flag[tuple(phys_domain)]

        def new_indices(dvik, iuv, indices, dist_view):
            new_ind = copy.deepcopy(indices)
            ind = np.where(dist_view[tuple(indices)] > dvik)
            i = 1
            for j in range(self.dim):
                if j != iuv:
                    new_ind[j + 1] = ind[i]
                    i += 1
            return new_ind

        s = self.stencil
        uvels = [s.uvx, s.uvy, s.uvz]

        for iuvel, uvel in enumerate(uvels[:self.dim]):
            for k, vk in np.ndenumerate(uvel):
                indices = [k] + [slice(None)]*self.dim
                if vk < 0 and label[2*iuvel] != -2:
                    for i in range(-vk):
                        indices[iuvel + 1] = i
                        dvik = -(i + .5)/vk
                        nind = new_indices(dvik, iuvel, indices, dist_view)
                        dist_view[tuple(nind)] = dvik
                        flag_view[tuple(nind)] = label[2*iuvel]
                elif vk > 0 and label[2*iuvel + 1] != -2:
                    for i in range(vk):
                        indices[iuvel + 1] = -i -1
                        dvik = (i + .5)/vk
                        nind = new_indices(dvik, iuvel, indices, dist_view)
                        dist_view[tuple(nind)] = dvik
                        flag_view[tuple(nind)] = label[2*iuvel+1]

    #pylint: disable=too-many-locals
    def __add_elem(self, elem):
        """
        Add an element

            - if elem.isfluid = False as a solid part. (bw=0)
            - if elem.isfluid = True as a fluid part.  (bw=1)

        FIX: this function works only for a 2D problem.
             Need to be improved and implement for the 3D.
        """
        # compute the box around the element adding vmax safety points
        vmax = self.stencil.vmax
        elem_bl, elem_ur = elem.get_bounds()
        phys_bl, _ = self.get_bounds_halo()

        tmp = np.array((elem_bl - phys_bl)/self.dx, np.int) - vmax
        nmin = np.maximum(vmax, tmp)
        tmp = np.array((elem_ur - phys_bl)/self.dx, np.int) + vmax + 1
        nmax = np.minimum(vmax + self.shape_in, tmp)

        # set the grid
        space_slice = [slice(imin, imax) for imin, imax in zip(nmin, nmax)]
        total_slice = [slice(None)] + space_slice
        # local view of the arrays
        ioo_view = self.in_or_out[tuple(space_slice)]
        dist_view = self.distance[tuple(total_slice)]
        flag_view = self.flag[tuple(total_slice)]

        tcoords = (self.coords_halo[d][s] for d, s in enumerate(space_slice))
        grid = np.meshgrid(*tcoords, sparse=True, indexing='ij')

        if not elem.isfluid: # add a solid part
            ind_solid = elem.point_inside(grid)
            ind_fluid = np.logical_not(ind_solid)
            ioo_view[ind_solid] = self.valout
        else: # add a fluid part
            ind_fluid = elem.point_inside(grid)
            ind_solid = np.logical_not(ind_fluid)
            ioo_view[ind_fluid] = self.valin

        for k in range(self.stencil.unvtot):
            vk = np.asarray(self.stencil.unique_velocities[k].v)
            if np.any(vk != 0):
                space_slice = [slice(imin + vk[d], imax + vk[d]) for imin, imax, d in zip(nmin, nmax, range(self.dim))]
                # check the cells that are out when we move with the vk velocity
                out_cells = self.in_or_out[tuple(space_slice)] == self.valout
                # compute the distance and set the boundary label
                # of each cell and the element with the vk velocity
                alpha, border = elem.distance(grid, self.dx*vk, 1.)
                # take the indices where the distance is lower than 1
                # between a fluid cell and the border of the element
                # with the vk velocity
                indx = np.logical_and(alpha > 0, ind_fluid)
                if out_cells.size != 0:
                    indx = np.logical_and(indx, out_cells)

                if elem.isfluid:
                    # take all points in the fluid in the ioo_view
                    indfluidinbox = ioo_view == self.valin
                    # take all the fluid points in the box (not only in the created element)
                    # which always are in fluid after a displacement of the velocity vk
                    border_to_interior = np.logical_and(np.logical_not(out_cells), indfluidinbox)
                    dist_view[k][border_to_interior] = self.valin
                    flag_view[k][border_to_interior] = self.valin
                else:
                    dist_view[k][ind_solid] = self.valin
                    flag_view[k][ind_solid] = self.valin

                #set distance
                ind4 = np.where(indx)
                if not elem.isfluid:
                    ind3 = np.where(alpha[ind4] < dist_view[k][ind4])[0]
                else:
                    ind3 = np.where(np.logical_or(alpha[ind4] > dist_view[k][ind4], dist_view[k][ind4] == self.valin))[0]

                ind = [i[ind3] for i in ind4]
                dist_view[k][tuple(ind)] = alpha[tuple(ind)]
                flag_view[k][tuple(ind)] = border[tuple(ind)]

    def list_of_labels(self):
        """
        Get the list of all the labels used in the geometry.
        """
        labels = np.unique(self.box_label)
        return np.union1d(labels, self.geom.list_of_elements_labels())

    #pylint: disable=too-many-locals, too-many-branches, too-many-nested-blocks, too-many-statements
    def visualize(self,
                  viewer_app=viewer.matplotlib_viewer,
                  view_distance=False,
                  view_in=True,
                  view_out=True,
                  view_bound=False,
                  label=None):
        """
        Visualize the domain by creating a plot.

        Parameters
        ----------
        viewer_app : Viewer, optional
            define the viewer to plot the domain
            default is viewer.matplotlib_viewer
        view_distance : boolean or int or list, optional
            view the distance between the interior points and the border
            default is False
        view_in : boolean, optional
            view the inner points
            default is True
        view_out : boolean, optional
            view the outer points
            default is True
        view_bound : boolean, optional
            view the points on the bounds
            default is False
        label : int or list, optional
            view the distance only for the specified labels

        Returns
        -------
        a figure representing the domain

        """
        # TODO: rewrite this method (it's too long)

        fig = viewer_app.Fig(dim=self.dim)
        view = fig[0]

        if isinstance(view_distance, bool):
            view_seg = view_distance
            if view_seg:
                view_distance = list(range(self.stencil.unvtot))
            else:
                view_distance = []
        elif isinstance(view_distance, int):
            view_seg = True
            view_distance = (view_distance,)
        elif isinstance(view_distance, (list, tuple)):
            view_seg = True
        else:
            s = "Error in visualize (domain): \n"
            s += "optional parameter view_distance should be\n"
            s += "  boolean, integer, list or tuple\n"
            log.error(s)

        if isinstance(view_bound, bool):
            view_bnd = view_bound
            if view_bnd:
                view_bound = list(range(self.stencil.unvtot))
            else:
                view_bound = []
        elif isinstance(view_bound, int):
            view_bnd = True
            view_bound = (view_bound,)
        elif isinstance(view_bound, (list, tuple)):
            view_bnd = True
        else:
            s = "Error in visualize (domain): \n"
            s += "optional parameter view_bound should be\n"
            s += "  boolean, integer, list or tuple\n"
            log.error(s)

        if self.dim == 1:
            x = self.coords_halo[0]
            y = np.zeros(x.shape)
            if view_seg or view_bound:
                vxkmax = self.stencil.vmax[0]
                for k in range(self.stencil.unvtot):
                    vxk = self.stencil.unique_velocities[k].vx
                    color = (1.-(vxkmax+vxk)*0.5/vxkmax, 0., (vxkmax+vxk)*0.5/vxkmax)
                    indbord = np.where(self.distance[k, :] <= 1)[0]
                    if indbord.size != 0:
                        xbound = x[indbord]
                        ybound = y[indbord]
                        dist = self.distance[k, indbord]
                        dx = self.dx
                        if view_seg:
                            lines = np.empty((2*xbound.size, 2))
                            lines[::2, :] = np.asarray([xbound, ybound]).T
                            lines[1::2, :] = np.asarray([xbound + dx*dist*vxk, ybound]).T
                            view.segments(lines, alpha=0.75, width=2, color=color)
                        if view_bound:
                            lines = np.asarray([xbound + dx*dist*vxk, ybound]).T
                            view.markers(lines, 200*self.dx, symbol='d', color=color)
            if view_in:
                indin = np.where(self.in_or_out == self.valin)
                view.markers(np.asarray([x[indin], y[indin]]).T, 200*self.dx, symbol='o', alpha=0.5, color='navy')
            if view_out:
                indout = np.where(self.in_or_out == self.valout)
                view.markers(np.asarray([x[indout], y[indout]]).T, 200*self.dx, symbol='s', color='orange')

            xmin, xmax = self.geom.bounds[0][:]
            length = xmax - xmin
            height = length/20
            view.axis(xmin - length/2, xmax + length/2, -10*height, 10*height)
            view.grid(visible=True)
            view.yaxis_set_visible(False)

        elif self.dim == 2:
            if not view_seg and not view_bnd:
                xmax, ymax = self.in_or_out.shape
                xmax -= 1
                ymax -= 1
                xpercent = 0.05*xmax
                ypercent = 0.05*ymax
                view.axis(-xpercent, xmax + xpercent, -ypercent, ymax + ypercent)
                view.image(self.in_or_out.transpose() >= 0)
                view.grid(False)
                view.xaxis_set_visible(False)
                view.yaxis_set_visible(False)
            else:
                xmin, xmax = self.geom.bounds[0][:]
                ymin, ymax = self.geom.bounds[1][:]

                xpercent = 2*self.dx
                ypercent = 2*self.dx
                view.axis(xmin-xpercent, xmax+xpercent, ymin-ypercent, ymax+ypercent, aspect='equal')

                x, y = self.coords_halo
                dx = self.dx
                vxkmax, vykmax = self.stencil.vmax

                for k in view_distance:
                    vxk = self.stencil.unique_velocities[k].vx
                    vyk = self.stencil.unique_velocities[k].vy
                    color = (1.-(vxkmax+vxk)*0.5/vxkmax, (vykmax+vyk)*0.25/vykmax, (vxkmax+vxk)*0.25/vxkmax)
                    if label is not None:
                        dummy = np.zeros(self.distance.shape[1:])
                        if isinstance(label, int):
                            dummy = np.logical_or(dummy, self.flag[k, :] == label)
                        elif isinstance(label, (tuple, list)):
                            for labelk in label:
                                dummy = np.logical_or(dummy, self.flag[k, :] == labelk)
                        else:
                            log.error("Error in visualize (domain): wrong type for optional argument label")
                    else:
                        dummy = np.ones(self.distance.shape[1:])
                    dummy = np.logical_and(dummy, self.distance[k, :] <= 1)
                    indbordx, indbordy = np.where(dummy)
                    if indbordx.size != 0:
                        dist = self.distance[k, indbordx, indbordy]
                        xbound = x[indbordx]
                        ybound = y[indbordy]
                        lines = np.empty((2*xbound.size, 2))
                        lines[::2, :] = np.asarray([xbound, ybound]).T
                        lines[1::2, :] = np.asarray([xbound + dx*dist*vxk, ybound + dx*dist*vyk]).T
                        view.segments(lines, alpha=0.75, width=2, color=color)

                for k in view_bound:
                    vxk = self.stencil.unique_velocities[k].vx
                    vyk = self.stencil.unique_velocities[k].vy
                    color = (1.-(vxkmax+vxk)*0.5/vxkmax, (vykmax+vyk)*0.25/vykmax, (vxkmax+vxk)*0.25/vxkmax)
                    if label is not None:
                        dummy = np.zeros(self.distance.shape[1:])
                        if isinstance(label, int):
                            dummy = np.logical_or(dummy, self.flag[k, :] == label)
                        elif isinstance(label, (tuple, list)):
                            for labelk in label:
                                dummy = np.logical_or(dummy, self.flag[k, :] == labelk)
                        else:
                            log.error("Error in visualize (domain): wrong type for optional argument label")
                    else:
                        dummy = np.ones(self.distance.shape[1:])
                    dummy = np.logical_and(dummy, self.distance[k, :] <= 1)
                    indbordx, indbordy = np.where(dummy)
                    if indbordx.size != 0:
                        dist = self.distance[k, indbordx, indbordy]
                        xbound = x[indbordx]
                        ybound = y[indbordy]
                        lines = np.asarray([xbound + dx*dist*vxk, ybound + dx*dist*vyk]).T
                        view.markers(lines, 200*self.dx, symbol='d', color=color)

                if view_in:
                    indinx, indiny = np.where(self.in_or_out == self.valin)
                    view.markers(np.asarray([x[indinx], y[indiny]]).T, 500*self.dx, symbol='o', alpha=0.5, color='navy')
                if view_out:
                    indoutx, indouty = np.where(self.in_or_out == self.valout)
                    view.markers(np.asarray([x[indoutx], y[indouty]]).T, 500*self.dx, symbol='s', color='orange')

        elif self.dim == 3:
            x, y, z = self.coords_halo
            dx = self.dx
            xmin, xmax = self.geom.bounds[0][:]
            ymin, ymax = self.geom.bounds[1][:]
            zmin, zmax = self.geom.bounds[2][:]

            if view_in:
                indinx, indiny, indinz = np.where(self.in_or_out == self.valin)
                view.markers(np.asarray([x[indinx], y[indiny], z[indinz]]).T, 50*self.dx**2, symbol='o', alpha=0.5, color='navy')
            if view_out:
                indoutx, indouty, indoutz = np.where(self.in_or_out == self.valout)
                view.markers(np.asarray([x[indoutx], y[indouty], z[indoutz]]).T, 100*self.dx**2, symbol='s', color='orange')
            view.set_label("X", "Y", "Z")
            if view_seg or view_bnd:
                vxkmax, vykmax, vzkmax = self.stencil.vmax
                for k in view_distance:
                    vxk = self.stencil.unique_velocities[k].vx
                    vyk = self.stencil.unique_velocities[k].vy
                    vzk = self.stencil.unique_velocities[k].vz
                    color = (1.-(vxkmax+vxk)*0.5/vxkmax, (vykmax+vyk)*0.25/vykmax, (vzkmax+vzk)*0.25/vzkmax)
                    if label is not None:
                        dummy = np.zeros(self.distance.shape[1:])
                        if isinstance(label, int):
                            dummy = np.logical_or(dummy, self.flag[k, :] == label)
                        elif isinstance(label, (tuple, list)):
                            for labelk in label:
                                dummy = np.logical_or(dummy, self.flag[k, :] == labelk)
                        else:
                            log.error("Error in visualize (domain): wrong type for optional argument label")
                    else:
                        dummy = np.ones(self.distance.shape[1:])
                    dummy = np.logical_and(dummy, self.distance[k, :] <= 1)
                    indbordx, indbordy, indbordz = np.where(dummy)
                    if indbordx.size != 0:
                        dist = self.distance[k, indbordx, indbordy, indbordz]
                        xbound = x[indbordx]
                        ybound = y[indbordy]
                        zbound = z[indbordz]
                        lines = np.empty((2*xbound.size, 3))
                        lines[::2, :] = np.asarray([xbound, ybound, zbound]).T
                        lines[1::2, :] = np.asarray([xbound + dx*dist*vxk, ybound + dx*dist*vyk, zbound + dx*dist*vzk]).T
                        view.segments(lines, alpha=0.75, color=color, width=2)
                for k in view_bound:
                    vxk = self.stencil.unique_velocities[k].vx
                    vyk = self.stencil.unique_velocities[k].vy
                    vzk = self.stencil.unique_velocities[k].vz
                    color = (1.-(vxkmax+vxk)*0.5/vxkmax, (vykmax+vyk)*0.25/vykmax, (vzkmax+vzk)*0.25/vzkmax)
                    if label is not None:
                        dummy = np.zeros(self.distance.shape[1:])
                        if isinstance(label, int):
                            dummy = np.logical_or(dummy, self.flag[k, :] == label)
                        elif isinstance(label, (tuple, list)):
                            for labelk in label:
                                dummy = np.logical_or(dummy, self.flag[k, :] == labelk)
                        else:
                            log.error("Error in visualize (domain): wrong type for optional argument label")
                    else:
                        dummy = np.ones(self.distance.shape[1:])
                    dummy = np.logical_and(dummy, self.distance[k, :] <= 1)
                    indbordx, indbordy, indbordz = np.where(dummy)
                    if indbordx.size != 0:
                        dist = self.distance[k, indbordx, indbordy, indbordz]
                        xbound = x[indbordx]
                        ybound = y[indbordy]
                        zbound = z[indbordz]
                        lines = np.asarray([xbound + dx*dist*vxk, ybound + dx*dist*vyk, zbound + dx*dist*vzk]).T
                        view.markers(lines, 100*self.dx**2, symbol='o', color=color)

            xpercent = 0.1*(xmax-xmin)
            ypercent = 0.1*(ymax-ymin)
            zpercent = 0.1*(zmax-zmin)
            view.axis(xmin - xpercent,
                      xmax + xpercent,
                      ymin - ypercent,
                      ymax + ypercent,
                      zmin - zpercent,
                      zmax+zpercent,
                      dim=3,
                      aspect='equal')

        else:
            log.error('Error in domain.visualize(): the dimension %d is not allowed', self.dim)

        view.title = "Domain"
        fig.show()
