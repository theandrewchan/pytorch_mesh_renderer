"""Differentiable triangle rasterizer."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import torch

import camera_utils
import rasterize_triangles_cpp


class BarycentricRasterizer(torch.autograd.Function):
    @staticmethod
    def forward(ctx, clip_space_vertices, triangles, image_width, image_height):
        """Rasterize the input mesh expressed in clip-space (xyzw) coordinates.

        Interpolates barycentric coordinates using perspective-correct interpolation
        and clips triangles that lie outside the viewing frustum.

        Args:
            clip_space_vertices: 2D float32 tensor of homogeneous vertices (xyzw)
                with shape [vertex_count, 4].
            triangles: 2D int32 tensor with shape [triangle_count, 3]. Each triplet
                should contain vertex indices describing a triangle such that the
                triangle's normal points toward the viewer if the forward order of
                the triplet defines a clockwise winding of the vertices. Gradients
                with respect to this tensor are not available.
            image_width: int specifying desired output image width in pixels.
            image_height: int specifying desired output image height in pixels.

        Returns:
            px_triangle_ids: A 2D tensor with shape [image_height, image_width].
              At return, each pixel contains a triangle id in the range
              [0, triangle_count). The id value is also 0 if there is no triangle
              at the pixel. The px_barycentric_coordinates must be checked to distinguish
              between the two cases.
            px_barycentric_coordinates: A 3D tensor with
              shape [image_height, image_width, 3]. At return, contains the triplet of
              barycentric coordinates at each pixel in the same vertex ordering as
              triangles. If no triangle is present, all coordinates are 0.
            z_buffer: A 2D tensor with shape [image_height, image_width] elements. At
              return, contains the normalized device Z coordinates of the rendered
              triangles.
        """
        px_triangle_ids, px_barycentric_coords, z_buffer = rasterize_triangles_cpp.forward(
            clip_space_vertices, triangles, image_width, image_height)
        ctx.save_for_backward(clip_space_vertices, triangles,
                              px_triangle_ids, px_barycentric_coords)
        return px_triangle_ids, px_barycentric_coords, z_buffer

    @staticmethod
    def backward(ctx, df_dbarycentric_coords):
        """Get the gradient of a scalar loss function w.r.t. input vertices
        expressed in clip-space (xyzw) coordinates.
        In the backward pass we receive a Tensor containing the gradient of the
        loss function w.r.t. our barycentric coordinate output and compute
        the gradient of the loss w.r.t. each vertex.

        Gradients w.r.t. triangle_ids or image width or height are not available.
        """
        clip_space_vertices, triangles, px_triangle_ids, px_barycentric_coords = ctx.saved_tensors
        output = rasterize_triangles_cpp.backward(
            df_dbarycentric_coords,
            clip_space_vertices,
            triangles,
            px_triangle_ids,
            px_barycentric_coords)
        df_dvertices, = output
        return df_dvertices, torch.zeros_like(triangles), 0, 0


def rasterize(world_space_vertices, attributes, triangles,
            camera_matrices, image_width, image_height, background_value):
    """Rasterize a mesh and compute interpolated vertex attributes.

    Applies projection matrices and then calls rasterize_clip_space().

    Args:
        world_space_vertices: 3D float32 tensor of xyz positions with shape
            [batch_size, vertex_count, 3].
        attributes: 3D float32 tensor with shape [batch_size, vertex_count,
            attribute_count]. Each vertex attribute is interpolated across
            the triangle using barycentric interpolation.
        triangles: 2D int32 tensor with shape [triangle_count, 3]. Each triplet
            should contain vertex indices describing a triangle such that the
            triangle's normal points toward the viewer if the forward order of
            the triplet defines a clockwise winding of the vertices. Gradients
            with respect to this tensor are not available.
        camera_matrices: 3D float tensor with shape [batch_size, 4, 4] containing
            model-view-perspective projection matrices.
        image_width: int specifying desired output image width in pixels.
        image_height: int specifying desired output image height in pixels.
        background_value: a 1D float32 tensor with shape [attribute_count].
            Pixels that lie outside all triangles take this value.

    Returns:
        A 4D float32 tensor with shape [batch_size, image_height, image_width,
        attribute_count], containing the interpolated vertex attributes at each
        pixel.

    Raises:
        ValueError: An invalid argument to the method is detected.
    """
    clip_space_vertices = camera_utils.transform_homogeneous(
        camera_matrices, world_space_vertices)
    return rasterize_clip_space(clip_space_vertices, attributes, triangles,
                                image_width, image_height, background_value)


def rasterize_clip_space(clip_space_vertices, attributes, triangles,
                         image_width, image_height, background_value):
    """Rasterize the input mesh expressed in clip-space (xyzw) coordinates.

    Interpolates vertex attributes using perspective-correct interpolation
    and clips triangles that lie outside the viewing frustum.

    Args:
        clip_space_vertices: 3D float32 tensor of homogeneous vertices (xyzw)
            with shape [batch_size, vertex_count, 4].
        attributes: 3D float32 tensor with shape [batch_size, vertex_count,
            attribute_count]. Each vertex attribute is interpolated across the
            triangle using barycentric interpolation.
        triangles: 2D int32 tensor with shape [triangle_count, 3]. Each triplet
            should contain vertex indices describing a triangle such that the
            triangle's normal points toward the viewer if the forward order of
            the triplet defines a clockwise winding of the vertices. Gradients
            with respect to this tensor are not available.
        image_width: int specifying desired output image width in pixels.
        image_height: int specifying desired output image height in pixels.
        background_value: a 1D float32 tensor with shape [attribute_count].
            Pixels that lie outside all triangles take this value.

    Returns:
        A 4D float32 tensor with shape [batch_size, image_height, image_width,
        attribute_count], containing the interpolated vertex attributes at each
        pixel.

    Raises:
        ValueError: An invalid argument to the method is detected.
    """
    if not image_width > 0:
        raise ValueError("Image width must be > 0.")
    if not image_height > 0:
        raise ValueError("Image height must be > 0.")
    if len(clip_space_vertices.shape) != 3:
        raise ValueError("The vertex buffer must be 3D.")

    vertex_count = clip_space_vertices.shape[1]

    batch_size = clip_space_vertices.shape[0]

    per_image_barycentric_coordinates = []
    per_image_vertex_ids = []

    for b in range(batch_size):
        px_triangle_ids, px_barycentric_coords, _ = BarycentricRasterizer.apply(
            clip_space_vertices[b, :, :], triangles, image_width, image_height)
        per_image_barycentric_coordinates.append(
            torch.reshape(px_barycentric_coords, [-1, 3])) # [pixel_count, 3]

        vertex_ids = torch.index_select(
            triangles, 0, torch.reshape(px_triangle_ids, [-1]).long()) # [pixel_count, 3]
        reindexed_ids = vertex_ids + b * clip_space_vertices.shape[1]
        per_image_vertex_ids.append(reindexed_ids)

    barycentric_coordinates = torch.reshape(
        torch.stack(per_image_barycentric_coordinates, 0), [-1, 3])
    vertex_ids = torch.reshape(
        torch.stack(per_image_vertex_ids, 0), [-1, 3])

    # Indexes with each pixel's clip-space triangle's extrema (the pixel's
    # 'corner points') ids to get the relevant properties for deferred shading.
    flattened_vertex_attributes = torch.reshape(attributes,
                                                [batch_size * vertex_count, -1])
    corner_attributes = flattened_vertex_attributes[vertex_ids.long()]

    # Computes the pixel attributes by interpolating the known attributes at
    # the corner points of the triangle interpolated with the
    # barycentric coordinates.
    weighted_vertex_attributes = torch.mul(corner_attributes,
        torch.unsqueeze(barycentric_coordinates, 2))
    summed_attributes = torch.sum(weighted_vertex_attributes, dim=1)
    attribute_images = torch.reshape(summed_attributes,
        [batch_size, image_height, image_width, -1])

    # Barycentric coordinates should approximately sum to one where there is
    # rendered geometry, but be exactly zero where there is not.
    alphas = torch.clamp(
        torch.sum(2.0 * barycentric_coordinates, dim=1), 0.0, 1.0)
    alphas = torch.reshape(alphas, [batch_size, image_height, image_width, 1])

    attributes_with_background = (
        alphas * attribute_images + (1.0 - alphas) * background_value)

    return attributes_with_background
