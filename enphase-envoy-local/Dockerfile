ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python dependencies (HTTP is stdlib urllib; only MQTT needs a wheel)
RUN pip3 install --no-cache-dir --break-system-packages \
    paho-mqtt==1.6.1

# Copy application and S6 service definitions
COPY rootfs /

# Set permissions on S6 scripts and the app
RUN chmod a+x /etc/services.d/enphase-envoy/run \
    && chmod a+x /etc/services.d/enphase-envoy/finish \
    && chmod a+x /opt/enphase/enphase_envoy.py
