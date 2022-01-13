# include parent path
import os
import sys
import numpy as np
import math
import ctypes

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import warp as wp
import render

np.random.seed(42)

wp.init()
wp.config.verify_cuda = True

@wp.kernel
def sample_mesh_query(mesh: wp.uint64,
                query_points: wp.array(dtype=wp.vec3),
                query_faces: wp.array(dtype=int),
                query_signs: wp.array(dtype=float),
                query_dist: wp.array(dtype=float)):
    
    tid = wp.tid()

    face_index = int(0)
    face_u = float(0.0)
    face_v = float(0.0)
    sign = float(0.0)

    max_dist = 10012.0

    p = query_points[tid]
    
    wp.mesh_query_point(mesh, p, max_dist, sign, face_index, face_u, face_v)
        
    cp = wp.mesh_eval_position(mesh, face_index, face_u, face_v)

    query_signs[tid] = sign
    query_faces[tid] = face_index
    query_dist[tid] = wp.length(cp-p)


@wp.func
def triangle_closest_point(a: wp.vec3, b: wp.vec3, c: wp.vec3, p: wp.vec3):
    ab = b - a
    ac = c - a
    ap = p - a

    d1 = wp.dot(ab, ap)
    d2 = wp.dot(ac, ap)

    if (d1 <= 0.0 and d2 <= 0.0):
        return wp.vec3(1.0, 0.0, 0.0)

    bp = p - b
    d3 = wp.dot(ab, bp)
    d4 = wp.dot(ac, bp)

    if (d3 >= 0.0 and d4 <= d3):
        return wp.vec3(0.0, 1.0, 0.0)

    vc = d1 * d4 - d3 * d2
    v = d1 / (d1 - d3)
    if (vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0):
        return wp.vec3(1.0 - v, v, 0.0)

    cp = p - c
    d5 = wp.dot(ab, cp)
    d6 = wp.dot(ac, cp)

    if (d6 >= 0.0 and d5 <= d6):
        return wp.vec3(0.0, 0.0, 1.0)

    vb = d5 * d2 - d1 * d6
    w = d2 / (d2 - d6)
    if (vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0):
        return wp.vec3(1.0 - w, 0.0, w)

    va = d3 * d6 - d5 * d4
    w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
    if (va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0):
        return wp.vec3(0.0, w, 1.0 - w)

    denom = 1.0 / (va + vb + vc)
    v = vb * denom
    w = vc * denom

    return wp.vec3(1.0 - v - w, v, w)

@wp.func
def solid_angle(v0: wp.vec3, v1: wp.vec3, v2: wp.vec3, p: wp.vec3):
    a = v0 - p
    b = v1 - p
    c = v2 - p

    a_len = wp.length(a)
    b_len = wp.length(b)
    c_len = wp.length(c)

    det = wp.dot(a, wp.cross(b, c))
    den = a_len*b_len*c_len + wp.dot(a, b)*c_len + wp.dot(b, c)*a_len + wp.dot(c, a)*b_len

    return wp.atan2(det, den)



@wp.kernel
def sample_mesh_brute(
                tri_points: wp.array(dtype=wp.vec3),
                tri_indices: wp.array(dtype=int),
                tri_count: int,
                query_points: wp.array(dtype=wp.vec3),
                query_faces: wp.array(dtype=int),
                query_signs: wp.array(dtype=float),
                query_dist: wp.array(dtype=float)):
    
    tid = wp.tid()

    min_face = int(0)
    min_dist = float(1.e+6)

    winding_angle = float(0.0)

    p = query_points[tid]

    for i in range(0, tri_count):
        
        a = tri_points[tri_indices[i*3+0]]
        b = tri_points[tri_indices[i*3+1]]
        c = tri_points[tri_indices[i*3+2]]

        winding_angle += solid_angle(a, b, c, p)

        bary = triangle_closest_point(a, b, c, p)

        cp = bary[0]*a + bary[1]*b + bary[2]*c
        cp_dist = wp.length(cp - p)

        if (cp_dist < min_dist):
            min_dist = cp_dist
            min_face = i
            
    query_faces[tid] = min_face
    query_signs[tid] = winding_angle 
    query_dist[tid] = min_dist


device = "cuda"
num_particles = 1000

sim_steps = 500
sim_dt = 1.0/60.0

sim_time = 0.0
sim_timers = {}
sim_render = True

sim_restitution = 0.0
sim_margin = 0.1

from pxr import Usd, UsdGeom, Gf, Sdf

# mesh = Usd.Stage.Open("./tests/assets/sphere.usda")
# mesh_geom = UsdGeom.Mesh(mesh.GetPrimAtPath("/Sphere/Sphere"))

# mesh = Usd.Stage.Open("./tests/assets/torus.usda")
# mesh_geom = UsdGeom.Mesh(mesh.GetPrimAtPath("/torus_obj/torus_obj"))

# mesh_points = wp.array(np.array(mesh_geom.GetPointsAttr().Get()), dtype=wp.vec3, device=device)
# mesh_indices = wp.array(np.array(mesh_geom.GetFaceVertexIndicesAttr().Get()), dtype=int, device=device)

mesh = Usd.Stage.Open("./tests/assets/torus_ov.usda")
mesh_geom = UsdGeom.Mesh(mesh.GetPrimAtPath("/World/Torus"))
# mesh = Usd.Stage.Open("./tests/assets/torus_ov_weld.usda")
# mesh_geom = UsdGeom.Mesh(mesh.GetPrimAtPath("/root/World/Torus/Torus"))
mesh_counts = mesh_geom.GetFaceVertexCountsAttr().Get()
mesh_indices = mesh_geom.GetFaceVertexIndicesAttr().Get()

num_tris = np.sum(np.subtract(mesh_counts, 2))
num_tri_vtx = num_tris * 3
tri_indices = np.zeros(num_tri_vtx, dtype=int)
ctr = 0
wedgeIdx = 0

for nb in mesh_counts:
    for i in range(nb-2):
        tri_indices[ctr] = mesh_indices[wedgeIdx]
        tri_indices[ctr + 1] = mesh_indices[wedgeIdx + i + 1]
        tri_indices[ctr + 2] = mesh_indices[wedgeIdx + i + 2]
        ctr+=3
    wedgeIdx+=nb

mesh_points = wp.array(np.array(mesh_geom.GetPointsAttr().Get()), dtype=wp.vec3, device=device)
mesh_indices = wp.array(np.array(tri_indices), dtype=int, device=device)


# create wp mesh
mesh = wp.Mesh(
    points=mesh_points, 
    velocities=None,
    indices=mesh_indices)

def particle_grid(dim_x, dim_y, dim_z, lower, radius, jitter):
    points = np.meshgrid(np.linspace(0, dim_x, dim_x), np.linspace(0, dim_y, dim_y), np.linspace(0, dim_z, dim_z))
    points_t = np.array((points[0], points[1], points[2])).T*radius*2.0 + np.array(lower)
    points_t = points_t + np.random.rand(*points_t.shape)*radius*jitter

    return points_t.reshape((-1, 3))

#p = ((np.random.rand(1000, 3) - np.array([0.5, 0.5, 0.5])))*10.0
p = particle_grid(32, 32, 32, np.array([-5.0, -5.0, -5.0]), 0.1, 0.1)*100.0
#p = particle_grid(32, 32, 32, np.array([-75.0, -25.0, -75.0]), 10.0, 0.0)
radius = 10.0

query_count = len(p)

query_points = wp.array(p, dtype=wp.vec3, device=device)

signs_query = wp.zeros(query_count, dtype=float, device=device)
faces_query = wp.zeros(query_count, dtype=int, device=device)
dist_query = wp.zeros(query_count, dtype=float, device=device)

signs_brute = wp.zeros(query_count, dtype=float, device=device)
faces_brute = wp.zeros(query_count, dtype=int, device=device)
dist_brute = wp.zeros(query_count, dtype=float, device=device)

wp.launch(kernel=sample_mesh_query, dim=query_count, inputs=[mesh.id, query_points, faces_query, signs_query, dist_query], device=device)
wp.launch(kernel=sample_mesh_brute, dim=query_count, inputs=[mesh_points, mesh_indices, int(len(mesh_indices)/3), query_points, faces_brute, signs_brute, dist_brute], device=device)

signs_query = signs_query.numpy()
faces_query = faces_query.numpy()
dist_query = dist_query.numpy()

signs_brute = signs_brute.numpy()
faces_brute = faces_brute.numpy()
dist_brute = dist_brute.numpy()

query_points = query_points.numpy()

#print(signs_query.numpy() - signs_brute.numpy())
#print(np.hstack([faces_query.numpy(), faces_brute.numpy()]))
#print(np.hstack([dist_query.numpy(), dist_brute.numpy()]))
print(np.hstack([signs_query, signs_brute]))

inside_query = []
inside_brute = []

for i in range(query_count):

    if (signs_query[i] < 0.0):
        inside_query.append(query_points[i].tolist())
    
    if (signs_brute[i] > 6.0):
        inside_brute.append(query_points[i].tolist())


stage = render.UsdRenderer("tests/outputs/test_mesh_query_point.usd")

stage.begin_frame(0.0)
stage.render_mesh(points=mesh_points.numpy(), indices=mesh_indices.numpy(), name="mesh")
stage.render_points(points=inside_query, radius=radius, name="query")
stage.render_points(points=inside_brute, radius=radius, name="brute")
stage.render_points(points=query_points, radius=radius, name="all")
stage.end_frame()

stage.save()