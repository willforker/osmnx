"""Truncate graph by distance, bounding box, or polygon."""

from __future__ import annotations

import logging as lg
from typing import TYPE_CHECKING

import networkx as nx

from . import convert
from . import utils
from . import utils_geo

if TYPE_CHECKING:
    from shapely.geometry import MultiPolygon
    from shapely.geometry import Polygon


def truncate_graph_dist(
    G: nx.MultiDiGraph,
    source_node: int,
    dist: float,
    *,
    weight: str = "length",
    retain_all: bool = False,
) -> nx.MultiDiGraph:
    """
    Remove every node farther than some network distance from `source_node`.

    This function can be slow for large graphs, as it must calculate shortest
    path distances between `source_node` and every other graph node.

    Parameters
    ----------
    G
        Input graph.
    source_node
        Node from which to measure network distances to all other nodes.
    dist
        Remove every node in the graph that is greater than `dist` distance
        (in same units as `weight` attribute) along the network from
        `source_node`.
    weight
        Graph edge attribute to use to measure distance.
    retain_all
        If True, return the entire graph even if it is not connected.
        Otherwise, retain only the largest weakly connected component.

    Returns
    -------
    G
        The truncated graph.
    """
    # get the shortest distance between the node and every other node
    distances = nx.shortest_path_length(G, source=source_node, weight=weight)

    # then identify every node further than dist away
    distant_nodes = {k for k, v in distances.items() if v > dist}
    unreachable_nodes = G.nodes - distances.keys()

    # make a copy to not mutate original graph object caller passed in
    G = G.copy()
    G.remove_nodes_from(distant_nodes | unreachable_nodes)

    # remove any isolated nodes and retain only the largest component (if
    # retain_all is True)
    if not retain_all:
        G = largest_component(remove_isolated_nodes(G))

    msg = f"Truncated graph by {weight}-weighted network distance"
    utils.log(msg, level=lg.INFO)
    return G


def truncate_graph_bbox(
    G: nx.MultiDiGraph,
    bbox: tuple[float, float, float, float],
    *,
    truncate_by_edge: bool = False,
    retain_all: bool = False,
) -> nx.MultiDiGraph:
    """
    Remove every node in graph that falls outside a bounding box.

    Parameters
    ----------
    G
        Input graph.
    bbox
        Bounding box as `(north, south, east, west)`.
    truncate_by_edge
        If True, retain nodes outside bounding box if at least one of node's
        neighbors is within the bounding box.
    retain_all
        If True, return the entire graph even if it is not connected.
        Otherwise, retain only the largest weakly connected component.

    Returns
    -------
    G
        The truncated graph.
    """
    # convert bounding box to a polygon, then truncate
    polygon = utils_geo.bbox_to_poly(bbox=bbox)
    G = truncate_graph_polygon(G, polygon, retain_all=retain_all, truncate_by_edge=truncate_by_edge)

    msg = "Truncated graph by bounding box"
    utils.log(msg, level=lg.INFO)
    return G


def truncate_graph_polygon(
    G: nx.MultiDiGraph,
    polygon: Polygon | MultiPolygon,
    *,
    retain_all: bool = False,
    truncate_by_edge: bool = False,
) -> nx.MultiDiGraph:
    """
    Remove every node in graph that falls outside a (Multi)Polygon.

    Parameters
    ----------
    G
        Input graph.
    polygon
        Only retain nodes in graph that lie within this geometry.
    retain_all
        If True, return the entire graph even if it is not connected.
        Otherwise, retain only the largest weakly connected component.
    truncate_by_edge
        If True, retain nodes outside boundary polygon if at least one of
        node's neighbors is within the polygon.

    Returns
    -------
    G
        The truncated graph.
    """
    msg = "Identifying all nodes that lie outside the polygon..."
    utils.log(msg, level=lg.INFO)

    # first identify all nodes whose point geometries lie within the polygon
    gs_nodes = convert.graph_to_gdfs(G, edges=False)["geometry"]
    to_keep = utils_geo._intersect_index_quadrats(gs_nodes, polygon)

    if len(to_keep) == 0:
        # no graph nodes within the polygon: can't create a graph from that
        msg = "Found no graph nodes within the requested polygon."
        raise ValueError(msg)

    # now identify all nodes whose point geometries lie outside the polygon
    gs_nodes_outside_poly = gs_nodes[~gs_nodes.index.isin(to_keep)]
    nodes_outside_poly = set(gs_nodes_outside_poly.index)

    if truncate_by_edge:
        # retain nodes outside boundary polygon if at least one of node's
        # neighbors is within the polygon
        nodes_to_remove = set()
        for node in nodes_outside_poly:
            # if all the neighbors of this node also lie outside polygon, then
            # mark this node for removal
            neighbors = set(G.successors(node)) | set(G.predecessors(node))
            if neighbors.issubset(nodes_outside_poly):
                nodes_to_remove.add(node)
    else:
        nodes_to_remove = nodes_outside_poly

    # now remove from the graph all those nodes that lie outside the polygon
    # make a copy to not mutate original graph object caller passed in
    G = G.copy()
    G.remove_nodes_from(nodes_to_remove)
    msg = f"Removed {len(nodes_to_remove):,} nodes outside polygon"
    utils.log(msg, level=lg.INFO)

    if not retain_all:
        G = largest_component(remove_isolated_nodes(G))

    msg = "Truncated graph by polygon"
    utils.log(msg, level=lg.INFO)
    return G


def remove_isolated_nodes(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """
    Remove from a graph all nodes that have no incident edges.

    Parameters
    ----------
    G
        Graph from which to remove isolated nodes.

    Returns
    -------
    G
        Graph with all isolated nodes removed.
    """
    # make a copy to not mutate original graph object caller passed in
    G = G.copy()

    # get the set of all isolated nodes, then remove them
    isolated_nodes = {node for node, degree in G.degree() if degree < 1}
    G.remove_nodes_from(isolated_nodes)

    msg = f"Removed {len(isolated_nodes):,} isolated nodes"
    utils.log(msg, level=lg.INFO)
    return G


def largest_component(G: nx.MultiDiGraph, *, strongly: bool = False) -> nx.MultiDiGraph:
    """
    Return subgraph of `G`'s largest weakly or strongly connected component.

    Parameters
    ----------
    G
        Input graph.
    strongly
        If True, return the largest strongly connected component. Otherwise
        return the largest weakly connected component.

    Returns
    -------
    G
        The largest connected component subgraph of the original graph.
    """
    if strongly:
        kind = "strongly"
        is_connected = nx.is_strongly_connected
        connected_components = nx.strongly_connected_components
    else:
        kind = "weakly"
        is_connected = nx.is_weakly_connected
        connected_components = nx.weakly_connected_components

    if not is_connected(G):
        # get all the connected components in graph then identify the largest
        largest_cc = max(connected_components(G), key=len)
        n = len(G)

        # induce (frozen) subgraph then unfreeze it by making new MultiDiGraph
        G = nx.MultiDiGraph(G.subgraph(largest_cc))

        msg = f"Got largest {kind} connected component ({len(G):,} of {n:,} total nodes)"
        utils.log(msg, level=lg.INFO)

    return G
