import os
import subprocess as sub
import tempfile
import time

from datetime import datetime

from sleap import Labels, Video
from sleap.nn import training
from sleap.nn.config import TrainingJobConfig

from typing import Any, Callable, Dict, List, Optional, Text

from PySide2 import QtWidgets

SKIP_TRAINING = False


def run_learning_pipeline(
    labels_filename: str,
    labels: Labels,
    training_jobs: Dict[Text, TrainingJobConfig],
    inference_params: Dict[str, Any],
    frames_to_predict: Dict[Video, List[int]] = None,
) -> int:
    """Run training (as needed) and inference.

    Args:
        labels_filename: Path to already saved current labels object.
        labels: The current labels object; results will be added to this.
        training_jobs: The TrainingJobs with params/hyperparams for training.
        inference_params: Parameters to pass to inference.
        frames_to_predict: Dict that gives list of frame indices for each video.

    Returns:
        Number of new frames added to labels.

    """

    save_viz = inference_params.get("_save_viz", False)

    # Train the TrainingJobs
    trained_jobs = run_gui_training(
        labels_filename, training_jobs, gui=True, save_viz=save_viz
    )

    # Check that all the models were trained
    if None in trained_jobs.values():
        return -1

    trained_job_paths = list(trained_jobs.values())

    # Run the Predictor for suggested frames
    new_labeled_frame_count = run_gui_inference(
        labels=labels,
        trained_job_paths=trained_job_paths,
        inference_params=inference_params,
        frames_to_predict=frames_to_predict,
        labels_filename=labels_filename,
    )

    return new_labeled_frame_count


def run_gui_training(
    labels_filename: str,
    training_jobs: Dict[Text, TrainingJobConfig],
    gui: bool = True,
    save_viz: bool = False,
) -> Dict[Text, Text]:
    """
    Run training for each training job.

    Args:
        labels: Labels object from which we'll get training data.
        training_jobs: Dict of the jobs to train.
        gui: Whether to show gui windows and process gui events.
        save_viz: Whether to save visualizations from training.

    Returns:
        Dictionary, keys are head name, values are path to trained config.
    """

    trained_jobs = dict()

    if gui:
        from sleap.nn.monitor import LossViewer
        from sleap.gui.imagedir import QtImageDirectoryWidget

        # open training monitor window
        win = LossViewer()
        win.resize(600, 400)
        win.show()

    for model_type, job in training_jobs.items():
        if getattr(job, "use_trained_model", False):
            # set path to TrainingJob already trained from previous run
            # json_name = f"{job.run_name}.json"
            trained_jobs[model_type] = job.outputs.run_path
            print(f"Using already trained model: {trained_jobs[model_type]}")

        else:
            # Update save dir and run name for job we're about to train
            # so we have accessgi to them here (rather than letting
            # train_subprocess update them).
            # training.Trainer.set_run_name(job, labels_filename)
            job.outputs.runs_folder = os.path.join(
                os.path.dirname(labels_filename), "models"
            )
            training.setup_new_run_folder(job.outputs)

            if gui:
                print("Resetting monitor window.")
                win.reset(what=str(model_type))
                win.setWindowTitle(f"Training Model - {str(model_type)}")
                if save_viz:
                    viz_window = QtImageDirectoryWidget.make_training_vizualizer(
                        job.outputs.run_path
                    )
                    viz_window.move(win.x() + win.width() + 20, win.y())
                    win.on_epoch.connect(viz_window.poll)

            print(f"Start training {str(model_type)}...")

            def waiting():
                if gui:
                    QtWidgets.QApplication.instance().processEvents()

            # Run training
            trained_job_path, success = train_subprocess(
                job,
                labels_filename,
                waiting_callback=waiting,
                update_run_name=False,
                save_viz=save_viz,
            )

            if success:
                # get the path to the resulting TrainingJob file
                trained_jobs[model_type] = trained_job_path
                print(f"Finished training {str(model_type)}.")
            else:
                if gui:
                    win.close()
                    QtWidgets.QMessageBox(
                        text=f"An error occurred while training {str(model_type)}. Your command line terminal may have more information about the error."
                    ).exec_()
                trained_jobs[model_type] = None

    if gui:
        # close training monitor window
        win.close()

    return trained_jobs


def run_gui_inference(
    labels: Labels,
    trained_job_paths: List[str],
    frames_to_predict: Dict[Video, List[int]],
    inference_params: Dict[str, str],
    labels_filename: str,
    gui: bool = True,
) -> int:
    """Run inference on specified frames using models from training_jobs.

    Args:
        labels: The current labels object; results will be added to this.
        trained_job_paths: List of paths to TrainingJobs with trained models.
        frames_to_predict: Dict that gives list of frame indices for each video.
        inference_params: Parameters to pass to inference.
        gui: Whether to show gui windows and process gui events.

    Returns:
        Number of new frames added to labels.
    """
    from sleap.nn import inference

    if gui:
        # show message while running inference
        progress = QtWidgets.QProgressDialog(
            f"Running inference on {len(frames_to_predict)} videos...",
            "Cancel",
            0,
            len(frames_to_predict),
        )
        progress.show()
        QtWidgets.QApplication.instance().processEvents()

    new_lfs = []
    for i, (video, frames) in enumerate(frames_to_predict.items()):

        if len(frames):

            def waiting():
                if gui:
                    QtWidgets.QApplication.instance().processEvents()
                    progress.setValue(i)
                    if progress.wasCanceled():
                        return -1

            # Run inference for desired frames in this video
            predictions_path, success = predict_subprocess(
                video=video,
                frames=frames,
                trained_job_paths=trained_job_paths,
                kwargs=inference_params,
                waiting_callback=waiting,
                labels_filename=labels_filename,
            )

            if success:
                predictions_labels = Labels.load_file(predictions_path, match_to=labels)
                new_lfs.extend(predictions_labels.labeled_frames)
            else:
                if gui:
                    progress.close()
                    QtWidgets.QMessageBox(
                        text=f"An error occcured during inference. Your command line terminal may have more information about the error."
                    ).exec_()
                return -1

    # Remove any frames without instances
    new_lfs = list(filter(lambda lf: len(lf.instances), new_lfs))

    # Merge predictions into current labels dataset
    _, _, new_conflicts = Labels.complex_merge_between(
        labels,
        new_labels=Labels(new_lfs),
        unify=False,  # since we used match_to when loading predictions file
    )

    # new predictions should replace old ones
    Labels.finish_complex_merge(labels, new_conflicts)

    # close message window
    if gui:
        progress.close()

    # return total_new_lf_count
    return len(new_lfs)


def train_subprocess(
    job_config: TrainingJobConfig,
    labels_filename: str,
    waiting_callback: Optional[Callable] = None,
    update_run_name: bool = True,
    save_viz: bool = False,
):
    """Runs training inside subprocess."""

    # run_name = job_config.outputs.run_name
    run_path = job_config.outputs.run_path

    success = False

    with tempfile.TemporaryDirectory() as temp_dir:

        # Write a temporary file of the TrainingJob so that we can respect
        # any changed made to the job attributes after it was loaded.
        temp_filename = datetime.now().strftime("%y%m%d_%H%M%S") + "_training_job.json"
        training_job_path = os.path.join(temp_dir, temp_filename)
        job_config.save_json(training_job_path)

        # Build CLI arguments for training
        cli_args = [
            "python",
            "-m",
            "sleap.nn.training",
            training_job_path,
            labels_filename,
            "--zmq",
            # "--run_name",
            # run_name,
        ]

        if save_viz:
            cli_args.append("--save_viz")

        print(cli_args)

        if not SKIP_TRAINING:
            # Run training in a subprocess
            with sub.Popen(cli_args) as proc:

                # Wait till training is done, calling a callback if given.
                while proc.poll() is None:
                    if waiting_callback is not None:
                        if waiting_callback() == -1:
                            # -1 signals user cancellation
                            return "", False
                    time.sleep(0.1)

                success = proc.returncode == 0

    print("Run Path:", run_path)

    return run_path, success


def predict_subprocess(
    video: "Video",
    trained_job_paths: List[str],
    kwargs: Dict[str, str],
    frames: Optional[List[int]] = None,
    waiting_callback: Optional[Callable] = None,
    labels_filename: Optional[str] = None,
):
    cli_args = ["python", "-m", "sleap.nn.inference"]

    if not trained_job_paths and "tracking.tracker" in kwargs and labels_filename:
        # No models so we must want to re-track previous predictions
        cli_args.append(labels_filename)
    else:
        cli_args.append(video.filename)

    # TODO: better support for video params
    if hasattr(video.backend, "dataset"):
        cli_args.extend(("--video.dataset", video.backend.dataset))

    if hasattr(video.backend, "input_format"):
        cli_args.extend(("--video.input_format", video.backend.input_format))

    # Make path where we'll save predictions
    output_path = ".".join(
        (video.filename, datetime.now().strftime("%y%m%d_%H%M%S"), "predictions.h5",)
    )

    for job_path in trained_job_paths:
        cli_args.extend(("-m", job_path))

    for key, val in kwargs.items():
        if not key.startswith("_"):
            cli_args.extend((f"--{key}", str(val)))

    cli_args.extend(("--frames", ",".join(map(str, frames))))

    cli_args.extend(("-o", output_path))

    print("Command line call:")
    print(" \\\n".join(cli_args))
    print()

    with sub.Popen(cli_args) as proc:
        while proc.poll() is None:
            if waiting_callback is not None:

                if waiting_callback() == -1:
                    # -1 signals user cancellation
                    return "", False

            time.sleep(0.1)

        print(f"Process return code: {proc.returncode}")
        success = proc.returncode == 0

    return output_path, success
