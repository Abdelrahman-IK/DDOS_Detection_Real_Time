# DDOS detection in real time
A method for detection of DoS/DDoS attacks based on an evaluation of the incoming/outgoing packet volume ratio and its variance to the long-time ratio.
# Usage:
- Using Spark, Kafka and IPFIXCol collector
Note: IPFIXCol collector used as a producer to send the data for Kafka

General App.py -iz <input-zookeeper-hostname>:<input-zookeeper-port> -it <input-topic> -oz <output-zookeeper-hostname>:<output-zookeeper-port> -ot <output-topic> -nf <regex for network range>
