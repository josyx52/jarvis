from runtime.processor_supervisor import ProcessorSupervisor


if __name__ == "__main__":

    supervisor = ProcessorSupervisor(
        partitions=8,
        min_processors=8,
        max_processors=16,
        scale_up_threshold=5000,
        scale_down_threshold=500
    )

    supervisor.run()