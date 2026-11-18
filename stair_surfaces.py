"""Horizontal sections of IFC solid meshes for conservative stair-open-access QA."""
import numpy as np
from shapely import build_area
from shapely.geometry import GeometryCollection, MultiLineString
from shapely.ops import unary_union
from shapely.errors import GEOSException


def horizontal_section(vertices, faces, height):
    """Closed solid section; unlike XY projection, a lintel does not close a door."""
    segments=[]
    triangles=vertices[faces]
    triangles=triangles[(triangles[:,:,2].min(axis=1)<height)&(triangles[:,:,2].max(axis=1)>height)]
    for tri in triangles:
        points=[]
        for i,j in ((0,1),(1,2),(2,0)):
            a,b=tri[i],tri[j]
            if (a[2]-height)*(b[2]-height)<0:
                t=(height-a[2])/(b[2]-a[2])
                points.append(tuple(np.round(a[:2]+t*(b[:2]-a[:2]),7)))
        if len(points)==2 and points[0]!=points[1]:segments.append(points)
    if not segments:return GeometryCollection()
    linework=unary_union(MultiLineString(segments))
    try:
        area=build_area(linework)
        return area if not area.is_empty else linework.buffer(.005)
    except GEOSException:
        # Invalid/overlapping mesh cuts must never be interpreted as an opening.
        hull=linework.convex_hull
        return hull if hull.area>0 else hull.buffer(.005)
