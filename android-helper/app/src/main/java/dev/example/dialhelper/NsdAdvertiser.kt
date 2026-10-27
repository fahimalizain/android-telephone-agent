package dev.example.dialhelper

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log

/**
 * Best-effort DNS-SD advertisement for the running dial helper using Android's
 * platform [NsdManager].
 *
 * NsdManager controls the advertised host record; this class sets only the
 * service type, instance name, port, and TXT attributes. Registration is
 * asynchronous, so a stop that arrives while registration is still in flight is
 * deferred until `onServiceRegistered`, and unregistration is attempted once.
 *
 * No extra permission is needed: NSD registration is performed by the system
 * service, and `INTERNET` is already declared for the HTTP listener.
 */
class NsdAdvertiser(
    context: Context,
    private val serviceName: String,
    private val port: Int,
    private val version: String
) {

    companion object {
        private const val TAG = "DialHelper"
        private const val SERVICE_TYPE = "_http._tcp."
    }

    private enum class State {
        IDLE,
        REGISTERING,
        REGISTERED,
        UNREGISTERING,
        FAILED,
        STOPPED
    }

    private val nsdManager =
        context.applicationContext.getSystemService(Context.NSD_SERVICE) as? NsdManager

    private val lock = Any()
    private var state = State.IDLE
    private var stopRequested = false

    private val registrationListener = object : NsdManager.RegistrationListener {
        override fun onServiceRegistered(serviceInfo: NsdServiceInfo) {
            val unregister: Boolean
            val actualName: String?
            synchronized(lock) {
                if (state != State.REGISTERING) return
                unregister = stopRequested
                actualName = if (unregister) null else serviceInfo.serviceName
                state = if (unregister) State.UNREGISTERING else State.REGISTERED
            }

            if (unregister) {
                unregisterService()
            } else {
                // Android may rename the instance on a conflict, for example
                // "android-telephone-agent-dialer (1)". Log the actual name.
                Log.i(
                    TAG,
                    "NSD advertising $actualName on port ${serviceInfo.port} ($SERVICE_TYPE)"
                )
            }
        }

        override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
            val stopping: Boolean
            synchronized(lock) {
                if (state != State.REGISTERING) return
                stopping = stopRequested
                state = State.STOPPED
            }
            if (!stopping) {
                Log.w(TAG, "NSD registration failed: ${errorName(errorCode)}")
            }
        }

        override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {
            synchronized(lock) {
                state = State.STOPPED
            }
            Log.i(TAG, "NSD advertisement stopped")
        }

        override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
            synchronized(lock) {
                state = State.STOPPED
            }
            Log.w(TAG, "NSD unregistration failed: ${errorName(errorCode)}")
        }
    }

    /** Starts registration once; failures leave the HTTP service untouched. */
    fun start() {
        val manager = nsdManager ?: run {
            Log.w(TAG, "NSD unavailable; mDNS advertisement disabled")
            return
        }

        synchronized(lock) {
            if (state != State.IDLE) return
            state = State.REGISTERING
        }

        val serviceInfo = NsdServiceInfo().apply {
            serviceName = this@NsdAdvertiser.serviceName
            serviceType = SERVICE_TYPE
            port = this@NsdAdvertiser.port
            setAttribute("path", "/")
            setAttribute("version", version)
        }

        try {
            manager.registerService(
                serviceInfo,
                NsdManager.PROTOCOL_DNS_SD,
                registrationListener
            )
        } catch (e: RuntimeException) {
            synchronized(lock) {
                state = State.FAILED
            }
            Log.w(TAG, "NSD registration failed: ${e.javaClass.simpleName}")
        }
    }

    /** Stops advertising, deferring unregistration until registration completes. */
    fun stop() {
        val unregister = synchronized(lock) {
            when (state) {
                State.IDLE, State.FAILED, State.STOPPED -> {
                    state = State.STOPPED
                    false
                }

                State.REGISTERING -> {
                    stopRequested = true
                    false
                }

                State.REGISTERED -> {
                    state = State.UNREGISTERING
                    true
                }

                State.UNREGISTERING -> false
            }
        }

        if (unregister) {
            unregisterService()
        }
    }

    private fun unregisterService() {
        val manager = nsdManager ?: return
        try {
            manager.unregisterService(registrationListener)
        } catch (e: RuntimeException) {
            synchronized(lock) {
                state = State.STOPPED
            }
            Log.w(TAG, "NSD unregistration failed: ${e.javaClass.simpleName}")
        }
    }

    private fun errorName(code: Int): String = when (code) {
        NsdManager.FAILURE_ALREADY_ACTIVE -> "already active"
        NsdManager.FAILURE_INTERNAL_ERROR -> "internal error"
        NsdManager.FAILURE_MAX_LIMIT -> "max limit"
        else -> "error $code"
    }
}
