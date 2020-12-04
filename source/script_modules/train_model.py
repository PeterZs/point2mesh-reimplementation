import time
import sys
import tensorflow as tf
import numpy as np
from tensorflow.python.keras.backend import dtype
import trimesh
import colorama
import os
from colorama import Fore, Back, Style
from ..mesh.Mesh import Mesh
from ..mesh.Obj import Obj
from ..model.PointToMeshModel import PointToMeshModel
from ..model.get_vertex_features import get_vertex_features
from ..loss.ChamferLossLayer import ChamferLossLayer
from ..loss.loss import BeamGapLossLayer, discrete_project
from ..mesh.remesh import remesh
from ..options.options import load_options

# This makes Colorama (terminal colors) work on Windows.
colorama.init()

# Set up CUDA.
physical_devices = tf.config.list_physical_devices("GPU")
if len(physical_devices) > 0:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

# Load the options and the point cloud.
options = load_options(sys.argv)
with open(options["point_cloud"], "r") as f:
    point_cloud_np = np.loadtxt(f)[:, :3]
point_cloud_tf = tf.convert_to_tensor(point_cloud_np, dtype=tf.float32)

# Create a function for saving meshes.
def save_mesh(file_name, vertices, faces):
    Obj.save(os.path.join(options["save_location"], file_name), vertices, faces)


# Create the mesh.
if options["initial_mesh"]:
    remeshed_vertices, remeshed_faces = Obj.load(options["initial_mesh"])
else:
    convex_hull = trimesh.convex.convex_hull(point_cloud_np)
    remeshed_vertices, remeshed_faces = remesh(
        convex_hull.vertices, convex_hull.faces, options["initial_num_faces"]
    )
save_mesh("tmp_initial_mesh.obj", remeshed_vertices, remeshed_faces)

# Create and train the model.
model = PointToMeshModel()
chamfer_loss = ChamferLossLayer()
beam_loss = BeamGapLossLayer(discrete_project)
optimizer = tf.keras.optimizers.Adam(learning_rate=0.00005)
num_subdivisions = options["num_subdivisions"]
new_vertices = None
for subdivision_level in range(num_subdivisions):
    # Subdivide the mesh if beyond the first level.
    if subdivision_level != 0:
        if new_vertices is None:
            raise Exception("Could not find vertices to subdivide.")
        new_num_faces = min(
            options["max_num_faces"],
            options["subdivision_multiplier"] * remeshed_faces.shape[0],
        )
        print(
            f"{Back.MAGENTA}Remeshing to {int(new_num_faces)} faces.{Style.RESET_ALL}"
        )
        remeshed_vertices, remeshed_faces = remesh(
            new_vertices.numpy(), remeshed_faces, new_num_faces
        )
    else:
        print(
            f"{Back.MAGENTA}Starting with {remeshed_faces.shape[0]} faces.{Style.RESET_ALL}"
        )
    mesh = Mesh(remeshed_vertices, remeshed_faces)

    # Create the random features.
    in_features = tf.random.uniform((mesh.edges.shape[0], 6), -0.5, 0.5)

    old_vertices = tf.convert_to_tensor(remeshed_vertices, dtype=tf.float32)
    num_iterations = options["num_iterations"]

    chamfer_losses = np.ones(100)
    chamfer_loss_iterator = 0
    chamfer_loss_terminator = False
    beamgap_losses = np.ones(25)
    beamgap_loss_iterator = 0
    beamgap_loss_terminator = False

    for iteration in range(num_iterations):
        iteration_start_time = time.time()
        if (not beamgap_loss_terminator) or (not chamfer_loss_terminator):
            with tf.GradientTape() as tape:
                # Get new vertex positions by calling the model.
                features = model(mesh, in_features)
                new_vertices = old_vertices + get_vertex_features(mesh, features)

                # Calculate loss.
                surface_sample = mesh.sample_surface(new_vertices, 10000)
                beamgap_modulo = options["beamgap_modulo"]
                if beamgap_modulo == -1:
                    use_beamgap_loss = False
                else:
                    use_beamgap_loss = iteration % beamgap_modulo == 0
                if use_beamgap_loss:
                    beam_loss.update_points_masks(mesh, new_vertices, point_cloud_tf)
                    total_loss = 0.01 * beam_loss(mesh, new_vertices)
                    beamgap_losses[beamgap_loss_iterator] = total_loss.numpy.item()
                    beamgap_loss_iterator = (beamgap_loss_iterator + 1) % 25
                    beamgap_loss_terminator = (
                        np.abs(
                            total_loss.numpy.item()
                            - beamgap_losses[beamgap_loss_iterator]
                        )
                        / beamgap_losses[beamgap_loss_iterator]
                        < 0.01
                    )
                else:
                    total_loss = chamfer_loss(surface_sample[0], point_cloud_tf)
                    chamfer_losses[chamfer_loss_iterator] = total_loss.numpy.item()
                    chamfer_loss_iterator = (chamfer_loss_iterator + 1) % 100
                    chamfer_loss_terminator = (
                        np.abs(
                            total_loss.numpy.item()
                            - chamfer_losses[chamfer_loss_iterator]
                        )
                        / chamfer_losses[chamfer_loss_iterator]
                        < 0.01
                    )

            # Apply gradients.
            gradients = tape.gradient(total_loss, model.trainable_variables)
            optimizer.apply_gradients(zip(gradients, model.trainable_variables))

            # Save the obj every few iterations.
            save_modulo = options["obj_save_modulo"]
            if iteration % save_modulo == 0:
                save_mesh(
                    f"tmp_{str(subdivision_level).zfill(2)}_{str(iteration).zfill(3)}.obj",
                    new_vertices.numpy(),
                    remeshed_faces,
                )

            # Log a progress update.
            loss_type = "Beam-gap" if use_beamgap_loss else "Chamfer"
            message = [
                f"{Back.WHITE}{Fore.BLACK}"
                f" {subdivision_level + 1}/{num_subdivisions} & {iteration + 1}/{num_iterations}",
                f"{Style.RESET_ALL}",
                f"{loss_type} Loss: {total_loss.numpy().item()},",
                f"Time: {time.time() - iteration_start_time}",
            ]
            print(" ".join(message))

print("Done.")
