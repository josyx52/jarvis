import sys

from processor.frame_processor import FrameProcessor


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: python run_processor.py <partition>")
        exit(1)

    partition = int(sys.argv[1])

    processor = FrameProcessor(partition)
    processor.run()