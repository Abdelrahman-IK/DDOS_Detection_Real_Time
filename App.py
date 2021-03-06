"""
Description: A method for detection of DoS/DDoS attacks based on an evaluation of
the incoming/outgoing packet volume ratio and its variance to the long-time (long window) ratio.
Usage:
  detection_ddos.py -iz <input-zookeeper-hostname>:<input-zookeeper-port> -it <input-topic>
  -oz <output-zookeeper-hostname>:<output-zookeeper-port> -ot <output-topic> -nf <regex for network range>
"""

import sys  # Common system functions
import os  # Common operating system functions
import argparse  # Arguments parser
import ujson as json  # Fast JSON parser
import socket  # Socket interface
import re  # Regular expression match

from termcolor import cprint  # Colors in the console output

from pyspark import SparkContext  # Spark API
from pyspark.streaming import StreamingContext  # Spark streaming API
from pyspark.streaming.kafka import KafkaUtils  # Spark streaming Kafka receiver

from kafka import KafkaProducer  # Kafka Python client


def send_to_kafka(data, producer, topic):
    """
    Send given data to the specified kafka topic.
    :param data: data to send
    :param producer: producer that sends the data
    :param topic: name of the receiving kafka topic
    """
    producer.send(topic, str(data))


def print_and_send(rdd, producer, topic):
    """
    Transform given computation results into the JSON format and send them to the specified host.
    JSON format:
        {"@type": "detection.ddos", "host" : <destination_ip> "shortratio" : <short-term ratio>,
        "longratio": <long-term ration>, "attackers": [set of attackers]}
    :param rdd: rdd to be parsed and sent
    :param producer: producer that sends the data
    :param topic: name of the receiving kafka topic
    """
    results = ""
    rdd_map = rdd.collectAsMap()

    # generate JSON response for each aggregated rdd
    for host, stats in rdd_map.iteritems():
        short_ratio = float(stats[0][0]) / stats[0][1]
        long_ratio = float(stats[1][0]) / stats[1][1]
        attackers = list(stats[0][2])

        new_entry = {"@type": "detection.ddos",
                     "dst_ip": host,
                     "shortratio": short_ratio,
                     "longratio": long_ratio,
                     "attackers": attackers}

        results += ("%s\n" % json.dumps(new_entry))

    # Print results to stdout
    cprint(results)

    # Send results to the specified kafka topic
    send_to_kafka(results, producer, topic)


def inspect_ddos(stream_data):
    """
    Main method performing the flows aggregation in short and long window and comparison of their ratios
    :type stream_data: Initialized spark streaming context.
    """
    # Create regex for monitored network
    local_ip_pattern = re.compile(network_filter)

    # Filter only the data with known source and destination IP
    filtered_stream_data = stream_data \
        .map(lambda x: json.loads(x[1])) \
        .filter(lambda json_rdd: ("ipfix.sourceIPv4Address" in json_rdd.keys() and
                                  "ipfix.destinationIPv4Address" in json_rdd.keys()
                                  ))

    # Create stream of base windows
    small_window = filtered_stream_data.window(base_window_length, base_window_length)

    # Count number of incoming packets from each source ip address for each destination ip address
    # from a given network range
    incoming_small_flows_stats = small_window \
        .filter(lambda json_rdd: re.match(local_ip_pattern, json_rdd["ipfix.destinationIPv4Address"])) \
        .map(lambda json_rdd: (json_rdd["ipfix.destinationIPv4Address"],
                               (json_rdd["ipfix.packetDeltaCount"], 0, {json_rdd["ipfix.sourceIPv4Address"]})))

    # Count number of outgoing packets for each source ip address from a given network range
    outgoing_small_flows_stats = small_window \
        .filter(lambda json_rdd: re.match(local_ip_pattern, json_rdd["ipfix.sourceIPv4Address"])) \
        .map(lambda json_rdd: (json_rdd["ipfix.sourceIPv4Address"],
                               (0, json_rdd["ipfix.packetDeltaCount"], set()))) \

    # Merge DStreams of incoming and outgoing number of packets
    small_window_aggregated = incoming_small_flows_stats.union(outgoing_small_flows_stats)\
        .reduceByKey(lambda actual, update: (actual[0] + update[0],
                                             actual[1] + update[1],
                                             actual[2].union(update[2])))

    # Create long window for long term profile
    union_long_flows = small_window_aggregated.window(long_window_length, base_window_length)
    long_window_aggregated = union_long_flows.reduceByKey(lambda actual, update: (actual[0] + update[0],
                                                          actual[1] + update[1])
                                                          )
    # Union DStreams with small and long window
    # RDD in DStream in format (local_device_IPv4, (
    # (short_inc_packets, short_out_packets, short_source_IPv4s),
    # (long_inc_packets, long_out_packets)))
    windows_union = small_window_aggregated.join(long_window_aggregated)

    # Filter out zero values to prevent division by zero
    nonzero_union = windows_union.filter(lambda rdd: rdd[1][0][1] != 0 and rdd[1][1][1] != 0)

    # Compare incoming and outgoing transfers volumes and filter only those suspicious
    # -> overreaching the minimal_incoming volume of packets and
    # -> short-term ratio is greater than long-term ratio * threshold
    windows_union_filtered = nonzero_union.filter(lambda rdd: rdd[1][0][0] > minimal_incoming and
                                                  float(rdd[1][0][0]) / rdd[1][0][1] > float(rdd[1][1][0]) /
                                                  rdd[1][1][1] * threshold
                                                  )

    # Return the detected records
    return windows_union_filtered


if __name__ == "__main__":
    # Prepare arguments parser (automatically creates -h argument).
    parser = argparse.ArgumentParser()
    parser.add_argument("-iz", "--input_zookeeper", help="input zookeeper hostname:port", type=str, required=True)
    parser.add_argument("-it", "--input_topic", help="input kafka topic", type=str, required=True)
    parser.add_argument("-oz", "--output_zookeeper", help="output zookeeper hostname:port", type=str, required=True)
    parser.add_argument("-ot", "--output_topic", help="output kafka topic", type=str, required=True)
    parser.add_argument("-nf", "--network_filter", help="regular expression filtering the watched IPs", type=str, required=True)

    # Parse arguments.
    args = parser.parse_args()

    # Set variables
    application_name = os.path.basename(sys.argv[0])  # Application name used as identifier
    kafka_partitions = 1  # Number of partitions of the input Kafka topic

    # Set method parameters:
    threshold = 50  # Minimal increase of receive/sent packets ratio
    minimal_incoming = 100000  # Minimal count of incoming packets
    long_window_length = 7200  # Window length for average ratio computation (must be a multiple of microbatch interval)
    base_window_length = 30  # Window length for basic computation (must be a multiple of microbatch interval)

    network_filter = args.network_filter  # Filter for network for detection (regex filtering), e.g. "10\.10\..+"

    # Spark context initialization
    sc = SparkContext(appName=application_name + " " + " ".join(sys.argv[1:]))  # Application name used as the appName
    ssc = StreamingContext(sc, 1)  # Spark microbatch is 1 second

    # Initialize input DStream of flows from specified Zookeeper server and Kafka topic
    input_stream = KafkaUtils.createStream(ssc, args.input_zookeeper, "spark-consumer-" + application_name,
                                           {args.input_topic: kafka_partitions})

    # Run the detection of ddos
    ddos_result = inspect_ddos(input_stream)

    # Initialize kafka producer
    kafka_producer = KafkaProducer(bootstrap_servers=args.output_zookeeper,
                                   client_id="spark-producer-" + application_name)

    # Process the results of the detection and send them to the specified host
    ddos_result.foreachRDD(lambda rdd: print_and_send(rdd, kafka_producer, args.output_topic))

    # Send any remaining buffered records
    kafka_producer.flush()

    # Start input data processing
    ssc.start()
    ssc.awaitTermination()