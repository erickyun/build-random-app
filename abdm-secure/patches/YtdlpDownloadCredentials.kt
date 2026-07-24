package ir.amirab.downloader.downloaditem.ytdlp

import arrow.core.Option
import arrow.core.getOrElse
import ir.amirab.downloader.downloaditem.IDownloadCredentials
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.net.URI

internal fun requireSafeYtdlpUrl(value: String) {
    require(value.isNotBlank()) { "URL must not be blank" }
    require(value == value.trim()) { "URL must not contain leading or trailing whitespace" }
    require(value.length <= 8192) { "URL is too long" }
    require(value.none { it.code < 0x20 || it.code == 0x7f }) { "URL contains control characters" }

    val uri = try {
        URI(value)
    } catch (error: Exception) {
        throw IllegalArgumentException("URL is not valid", error)
    }

    require(uri.scheme?.lowercase() in setOf("http", "https")) {
        "Only HTTP and HTTPS URLs are allowed"
    }
    require(!uri.host.isNullOrBlank()) { "URL must contain a valid host" }
    require(uri.userInfo == null) { "URLs containing embedded credentials are not allowed" }
}

@Serializable
@SerialName("ytdlp")
data class YtdlpDownloadCredentials(
    override val link: String,
    override val downloadPage: String? = null,
    val quality: YtdlpQuality = YtdlpQuality.Default,
) : IDownloadCredentials {
    override fun validateCredentials() {
        requireSafeYtdlpUrl(link)
    }

    override fun copy(
        link: Option<String>,
        downloadPage: Option<String?>
    ): IDownloadCredentials {
        return copy(
            link = link.getOrElse { this.link },
            downloadPage = downloadPage.getOrElse { this.downloadPage }
        )
    }
}
