import java.util.Properties
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

/** Escape a value so it is a valid Kotlin/Java string literal in BuildConfig. */
fun buildConfigString(value: String): String =
    "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

val keystorePropertiesFile = rootProject.file("keystore.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.isFile) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}
val hasReleaseKeystore = keystorePropertiesFile.isFile

// Precedence: Gradle property, environment variable, then the gitignored local
// keystore.properties. The last fallback keeps Android Studio builds working
// when the IDE does not inherit GRADLE_USER_HOME from the shell.
val dialHelperToken: String = providers.gradleProperty("dialHelperToken")
    .orElse(providers.environmentVariable("DIAL_HELPER_TOKEN"))
    .orElse(keystoreProperties.getProperty("dialHelperToken") ?: "")
    .get()
val effectiveApiToken: String = dialHelperToken.ifBlank { "REPLACE_WITH_32_BYTE_RANDOM_TOKEN" }
val dialHelperSimLabel: String = providers.gradleProperty("dialHelperSimLabel")
    .orElse(providers.environmentVariable("DIAL_HELPER_SIM_LABEL"))
    .orElse(keystoreProperties.getProperty("dialHelperSimLabel") ?: "")
    .get()

android {
    namespace = "dev.example.dialhelper"
    compileSdk = 36
    buildToolsVersion = "36.1.0"

    defaultConfig {
        applicationId = "dev.example.dialhelper"
        minSdk = 30
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"

        buildConfigField("String", "API_TOKEN", buildConfigString(effectiveApiToken))
        buildConfigField("String", "AGENT_SIM_LABEL", buildConfigString(dialHelperSimLabel))
    }

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        if (hasReleaseKeystore) {
            create("release") {
                storeFile = file(
                    keystoreProperties.getProperty("storeFile")
                        ?: error("keystore.properties is missing storeFile")
                )
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            if (hasReleaseKeystore) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}
