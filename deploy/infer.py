import sys
import os
__dir__ = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.abspath(os.path.join(__dir__, '../')))

import os
import paddle
from paddle import inference
import numpy as np
from PIL import Image
from paddle.vision import transforms
from tools.density import GaussianDensityPaddle


class InferenceEngine(object):
    """InferenceEngine

    Inference engina class which contains preprocess, run, postprocess
    """

    def __init__(self, args):
        """
        Args:
            args: Parameters generated using argparser.
        Returns: None
        """
        super().__init__()
        self.args = args

        # init inference engine
        self.predictor, self.config, self.input_tensor, self.output_tensor,self.density = self.load_predictor(
            os.path.join(args.model_dir, "inference.pdmodel"),
            os.path.join(args.model_dir, "inference.pdiparams"))

        # build transforms
        self.transforms = transforms.Compose([
                                transforms.Resize((args.resize_size, args.resize_size)),
                                transforms.ToTensor(),
                                transforms.Normalize(mean=[0.485, 0.456, 0.406],std=[0.229, 0.224, 0.225]),
                                ])

        # wamrup
        if self.args.warmup > 0:
            for idx in range(args.warmup):
                print(idx)
                x = np.random.rand(1, 3, self.args.resize_size,
                                   self.args.resize_size).astype("float32")
                self.input_tensor.copy_from_cpu(x)
                self.predictor.run()
                self.output_tensor.copy_to_cpu()
        return

    def load_predictor(self, model_file_path, params_file_path):
        """load_predictor
        initialize the inference engine
        Args:
            model_file_path: inference model path (*.pdmodel)
            model_file_path: inference parmaeter path (*.pdiparams)
        Return:
            predictor: Predictor created using Paddle Inference.
            config: Configuration of the predictor.
            input_tensor: Input tensor of the predictor.
            output_tensor: Output tensor of the predictor.
        """
        args = self.args
        config = inference.Config(model_file_path, params_file_path)
        if args.use_gpu:
            config.enable_use_gpu(1000, 0)
        else:
            config.disable_gpu()
            # The thread num should not be greater than the number of cores in the CPU.
            config.set_cpu_math_library_num_threads(4)

        # enable memory optim
        config.enable_memory_optim()
        config.disable_glog_info()

        config.switch_use_feed_fetch_ops(False)
        config.switch_ir_optim(True)

        # create predictor
        predictor = inference.create_predictor(config)

        # get input and output tensor property
        input_names = predictor.get_input_names()
        input_tensor = predictor.get_input_handle(input_names[0])

        output_names = predictor.get_output_names()
        output_tensor = predictor.get_output_handle(output_names[0])

        density = GaussianDensityPaddle()
        density.load(f'{args.model_dir}/model_'+args.data_type+'_dict_data.pkl')

        return predictor, config, input_tensor, output_tensor,density

    def preprocess(self, img_path):
        """preprocess
        Preprocess to the input.
        Args:
            img_path: Image path.
        Returns: Input data after preprocess.
        """
        with open(img_path, "rb") as f:
            img = Image.open(f)
            img = img.resize((self.args.resize_size,self.args.resize_size))
            img = img.convert("RGB")
        img = self.transforms(img)
        img = np.expand_dims(img, axis=0)
        return img

    def postprocess(self, x):
        """postprocess
        Postprocess to the inference engine output.
        Args:
            x: Inference engine output.
        Returns: Output data after argmax.
        """
        embed = paddle.to_tensor(x)
        embed = paddle.nn.functional.normalize(embed, p=2, axis=1)
        distances = self.density.predict(embed)
        if distances[0] >= self.density.best_threshold:
            class_id = 1 # 异常
            prob = distances.numpy()[0]
        else:
            class_id = 0 # 正常
            prob = distances.numpy()[0]
        return class_id, prob

    def run(self, x):
        """run
        Inference process using inference engine.
        Args:
            x: Input data after preprocess.
        Returns: Inference engine output
        """
        self.input_tensor.copy_from_cpu(x)
        self.predictor.run()
        output = self.output_tensor.copy_to_cpu()
        return output


def get_args(add_help=True):
    """
    parse args
    """
    import argparse

    def str2bool(v):
        return v.lower() in ("true", "t", "1")

    parser = argparse.ArgumentParser(
        description="PaddlePaddle Classification Training", add_help=add_help)

    parser.add_argument(
        "--model_dir", default="deploy", help="inference model dir")
    parser.add_argument(
        "--use_gpu", default=False, type=str2bool, help="use_gpu")
    parser.add_argument(
        "--max_batch-size", default=16, type=int, help="max_batch_size")
    parser.add_argument("--batch_size", default=1, type=int, help="batch size")
    parser.add_argument("--data_type", default="bottle", help="data type for the model")
    parser.add_argument(
        "--resize_size", default=256, type=int, help="resize_size")
    parser.add_argument("--crop_size", default=256, type=int, help="crop_szie")
    parser.add_argument("--img_path", default="demo/bottle_good.png")
    parser.add_argument(
        "--benchmark", default=False, type=str2bool, help="benchmark")
    parser.add_argument("--warmup", default=0, type=int, help="warmup iter")
    args = parser.parse_args()
    return args


def infer_main(args):
    """infer_main
    Main inference function.
    Args:
        args: Parameters generated using argparser.
    Returns:
        class_id: Class index of the input.
        prob: : Probability of the input.
    """
    inference_engine = InferenceEngine(args)

    # init benchmark
    if args.benchmark:
        import auto_log
        autolog = auto_log.AutoLogger(
            model_name="classification",
            batch_size=args.batch_size,
            inference_config=inference_engine.config,
            gpu_ids="auto" if args.use_gpu else None)

    assert args.batch_size == 1, "batch size just supports 1 now."

    # enable benchmark
    if args.benchmark:
        autolog.times.start()

    # preprocess
    img = inference_engine.preprocess(args.img_path)

    if args.benchmark:
        autolog.times.stamp()

    output = inference_engine.run(img)

    if args.benchmark:
        autolog.times.stamp()

    # postprocess
    class_id, prob = inference_engine.postprocess(output)

    if args.benchmark:
        autolog.times.stamp()
        autolog.times.end(stamp=True)
        autolog.report()

    print(f"image_name: {args.img_path}, data is {'anomaly' if class_id==1 else 'normal'}, score is {prob}, threshold is {inference_engine.density.best_threshold.numpy()[0]}")
    return class_id, prob


if __name__ == "__main__":
    args = get_args()
    class_id, prob = infer_main(args)