import time
import os
import logging
import json
from serial import Serial, SerialException
from prometheus_client import start_http_server, Gauge, Counter
import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Environment variables
MQTT_HOST = os.getenv('MQTT_HOST', 'localhost')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))
MQTT_TOPIC_PREFIX = os.getenv('MQTT_TOPIC_PREFIX', 'sensors/co2')
SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/ttyAMA0')
PROMETHEUS_PORT = int(os.getenv('PROMETHEUS_PORT', '9100'))

# CO2 Level Classifications
CO2_LEVELS = {
    'GREAT': (350, 450, 'Same as outdoor level'),
    'NORMAL': (451, 1000, 'Normal indoor level'),
    'SLEEPY': (1001, 2000, 'May cause drowsiness'),
    'WARNING': (2001, 5000, 'Warning level - Poor air quality'),
    'ALERT': (5001, float('inf'), 'ALERT - Dangerous level')
}

# Log environment variables
logger.info("Starting with configuration:")
logger.info(f"MQTT_HOST: {MQTT_HOST}")
logger.info(f"MQTT_PORT: {MQTT_PORT}")
logger.info(f"MQTT_TOPIC_PREFIX: {MQTT_TOPIC_PREFIX}")
logger.info(f"SERIAL_PORT: {SERIAL_PORT}")
logger.info(f"PROMETHEUS_PORT: {PROMETHEUS_PORT}")

# Prometheus metrics
co2_gauge = Gauge('co2_concentration_ppm', 'CO2 concentration in parts per million')
co2_level = Gauge('co2_level', 'CO2 level classification', ['level'])
co2_alerts = Counter('co2_alerts_total', 'Number of CO2 alerts by severity', ['severity'])

# Initialize CO2 level gauges
for level in CO2_LEVELS:
    co2_level.labels(level=level).set(0)

def get_co2_level(ppm):
    for level, (min_val, max_val, description) in CO2_LEVELS.items():
        if min_val <= ppm <= max_val:
            return level, description
    return 'ALERT', CO2_LEVELS['ALERT'][2]

# MQTT callbacks
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info("Successfully connected to MQTT broker")
    else:
        logger.error(f"Failed to connect to MQTT broker. Reason code: {reason_code}")

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    logger.warning(f"Disconnected from MQTT broker with result code: {reason_code}")
    if reason_code != 0:
        logger.error("Unexpected disconnection. Attempting to reconnect...")

def on_publish(client, userdata, mid, reason_code=0, properties=None):
    if reason_code == 0:
        logger.info(f"Message {mid} published successfully")
    else:
        logger.error(f"Failed to publish message {mid}. Reason code: {reason_code}")

def read_co2():
    try:
        logger.debug(f"Attempting to connect to serial port: {SERIAL_PORT}")
        sensor = Serial(
            port=SERIAL_PORT,
            baudrate=9600,
            bytesize=8,
            parity='N',
            stopbits=1,
            timeout=0.5
        )
        
        logger.info(f"Connected to sensor on port: {SERIAL_PORT}")
        
        # Clear input buffer and add a small delay
        sensor.flushInput()
        time.sleep(0.1)
        
        # Command to read CO2 value
        command = b"\xFE\x04\x00\x03\x00\x01\xd5\xc5"
        logger.debug(f"Sending command: {command.hex()}")
        
        # Write the command and add a small delay
        bytes_written = sensor.write(command)
        logger.debug(f"Wrote {bytes_written} bytes")
        sensor.flush()
        time.sleep(0.1)
        
        # Read response
        logger.debug("Waiting for response...")
        response = sensor.read(7)
        logger.debug(f"Received {len(response)} bytes: {response.hex() if response else 'no data'}")
        
        if len(response) == 7:
            co2_high = response[3]
            co2_low = response[4]
            co2_ppm = (co2_high * 256) + co2_low
            level, description = get_co2_level(co2_ppm)
            logger.info(f"CO2 reading: {co2_ppm} ppm - {level}: {description}")
            return co2_ppm
        else:
            logger.error("Invalid response length")
            logger.error("\nTroubleshooting suggestions:")
            logger.error("1. Verify the correct port")
            logger.error("2. Check physical connections")
            logger.error("3. Verify sensor settings")
            return None
            
    except SerialException as e:
        logger.error(f"Serial communication error: {e}")
        logger.error("\nPlease check:")
        logger.error("1. Are you running with proper permissions?")
        logger.error("2. Is the device plugged in?")
        logger.error("3. Do you have the correct port name?")
        return None
        
    finally:
        if 'sensor' in locals() and sensor.is_open:
            sensor.close()
            logger.debug("Serial port closed")

def connect_mqtt(client):
    """Attempt to connect to MQTT broker with retries"""
    max_retries = 3
    retry_count = 0
    retry_delay = 5  # seconds

    while retry_count < max_retries:
        try:
            logger.info(f"Attempting to connect to MQTT broker at {MQTT_HOST}:{MQTT_PORT} (attempt {retry_count + 1}/{max_retries})")
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            client.loop_start()
            return True
        except Exception as e:
            retry_count += 1
            if retry_count < max_retries:
                logger.warning(f"Failed to connect to MQTT broker: {e}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to MQTT broker after {max_retries} attempts: {e}")
                return False

# Global variable to track peak PPM
peak_ppm = 0

def main():
    global peak_ppm
    # Start prometheus HTTP server
    try:
        start_http_server(PROMETHEUS_PORT)
        logger.info(f"Prometheus metrics available on port {PROMETHEUS_PORT}")
    except Exception as e:
        logger.error(f"Failed to start Prometheus server: {e}")
        return
    
    # Setup MQTT client with VERSION2 API
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish
    
    # Set Last Will and Testament for online status
    online_topic = f"{MQTT_TOPIC_PREFIX}/online"
    client.will_set(online_topic, "0", qos=1, retain=True)
    
    if not connect_mqtt(client):
        logger.error("Failed to establish MQTT connection. Exiting.")
        return

    logger.info("Starting main loop")
    while True:
        try:
            co2_value = read_co2()
            
            if co2_value is not None:
                # Get CO2 level classification
                level, description = get_co2_level(co2_value)
                
                # Update Prometheus metrics
                co2_gauge.set(co2_value)
                
                # Reset all level gauges
                for l in CO2_LEVELS:
                    co2_level.labels(level=l).set(0)
                # Set current level to 1
                co2_level.labels(level=level).set(1)
                
                # Increment alert counter if necessary
                if level in ['WARNING', 'ALERT']:
                    co2_alerts.labels(severity=level).inc()
                
                # Publish to MQTT topics
                try:
                    # Topic 1: detected status
                    detected_topic = f"{MQTT_TOPIC_PREFIX}/detected"
                    detected_status = "NORMAL" if co2_value <= 1000 else "ABNORMAL"
                    result1 = client.publish(
                        detected_topic,
                        payload=detected_status,
                        qos=1,
                        retain=False
                    )
                    if result1.rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.error(f"Failed to publish to detected topic. Error code: {result1.rc}")
                    else:
                        result1.wait_for_publish()
                        logger.info(f"Published to {detected_topic}: {detected_status}")

                    # Topic 2: PPM level
                    level_topic = f"{MQTT_TOPIC_PREFIX}/level"
                    result2 = client.publish(
                        level_topic,
                        payload=str(co2_value),
                        qos=1,
                        retain=False
                    )
                    if result2.rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.error(f"Failed to publish to level topic. Error code: {result2.rc}")
                    else:
                        result2.wait_for_publish()
                        logger.info(f"Published to {level_topic}: {co2_value}")

                    # Topic 3: Peak PPM (only publish when new peak is reached)
                    if co2_value > peak_ppm:
                        peak_ppm = co2_value
                        peak_topic = f"{MQTT_TOPIC_PREFIX}/peak"
                        result3 = client.publish(
                            peak_topic,
                            payload=str(peak_ppm),
                            qos=1,
                            retain=True  # Retain the peak value
                        )
                        if result3.rc != mqtt.MQTT_ERR_SUCCESS:
                            logger.error(f"Failed to publish to peak topic. Error code: {result3.rc}")
                        else:
                            result3.wait_for_publish()
                            logger.info(f"New peak value published to {peak_topic}: {peak_ppm}")

                    # Topic 4: Online status - publish 1 for valid reading
                    online_topic = f"{MQTT_TOPIC_PREFIX}/online"
                    result4 = client.publish(
                        online_topic,
                        payload="1",
                        qos=1,
                        retain=True
                    )
                    if result4.rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.error(f"Failed to publish online status. Error code: {result4.rc}")
                    else:
                        result4.wait_for_publish()
                        logger.debug("Published online status: 1")

                except Exception as e:
                    logger.error(f"Exception while publishing to MQTT: {e}")
                
            else:
                # Publish offline status when no valid reading
                online_topic = f"{MQTT_TOPIC_PREFIX}/online"
                try:
                    result = client.publish(
                        online_topic,
                        payload="0",
                        qos=1,
                        retain=True
                    )
                    if result.rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.error(f"Failed to publish offline status. Error code: {result.rc}")
                    else:
                        result.wait_for_publish()
                        logger.debug("Published online status: 0")
                except Exception as e:
                    logger.error(f"Exception while publishing offline status: {e}")

            time.sleep(10)  # Wait 10 seconds before next reading
            
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            # Publish offline status on error
            try:
                client.publish(f"{MQTT_TOPIC_PREFIX}/online", "0", qos=1, retain=True)
            except Exception as publish_error:
                logger.error(f"Failed to publish offline status after error: {publish_error}")
            time.sleep(10)  # Wait before retrying

if __name__ == "__main__":
    try:
        logger.info("Starting S8 CO2 sensor application")
        logger.info("CO2 Level Classifications:")
        for level, (min_val, max_val, desc) in CO2_LEVELS.items():
            logger.info(f"  {level}: {min_val}-{max_val} ppm - {desc}")
        main()
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application crashed: {e}")
