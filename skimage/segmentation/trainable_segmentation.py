from itertools import combinations_with_replacement
import itertools
import numpy as np
from skimage import filters, feature
from skimage import img_as_float32
from concurrent.futures import ThreadPoolExecutor

try:
    import dask

    has_dask = True
except ImportError:
    has_dask = False

try:
    from sklearn.exceptions import NotFittedError

    has_sklearn = True
except ImportError:
    has_sklearn = False

    class NotFittedError(Exception):
        pass


def _texture_filter(gaussian_filtered):
    H_elems = [
        np.gradient(np.gradient(gaussian_filtered)[ax0], axis=ax1)
        for ax0, ax1 in combinations_with_replacement(range(gaussian_filtered.ndim), 2)
    ]
    eigvals = feature.hessian_matrix_eigvals(H_elems)
    return eigvals


def _singlescale_basic_features_singlechannel(
    img, sigma, intensity=True, edges=True, texture=True
):
    results = ()
    gaussian_filtered = filters.gaussian(img, sigma)
    if intensity:
        results += (gaussian_filtered,)
    if edges:
        results += (filters.sobel(gaussian_filtered),)
    if texture:
        results += (*_texture_filter(gaussian_filtered),)
    return results


def _mutiscale_basic_features_singlechannel(
    img,
    intensity=True,
    edges=True,
    texture=True,
    sigma_min=0.5,
    sigma_max=16,
    num_workers=None,
):
    """Features for a single channel nd image.

    Parameters
    ----------
    img : ndarray
        Input image, which can be grayscale or multichannel.
    intensity : bool, default True
        If True, pixel intensities averaged over the different scales
        are added to the feature set.
    edges : bool, default True
        If True, intensities of local gradients averaged over the different
        scales are added to the feature set.
    texture : bool, default True
        If True, eigenvalues of the Hessian matrix after Gaussian blurring
        at different scales are added to the feature set.
    sigma_min : float, optional
        Smallest value of the Gaussian kernel used to average local
        neighbourhoods before extracting features.
    sigma_max : float, optional
        Largest value of the Gaussian kernel used to average local
        neighbourhoods before extracting features.
    num_workers : int or None, optional
        The number of parallel threads to use. If set to ``None``, the full
        set of available cores are used

    Returns
    -------
    features : list
        List of features, each element of the list is an array of shape as img.
    """
    # computations are faster as float32
    img = np.ascontiguousarray(img_as_float32(img))
    sigmas = np.logspace(
        np.log2(sigma_min),
        np.log2(sigma_max),
        num=int(np.log2(sigma_max) - np.log2(sigma_min) + 1),
        base=2,
        endpoint=True,
    )[::-1]
    if has_dask:
        out_sigmas = [
            dask.delayed(_singlescale_basic_features_singlechannel)(
                img, s, intensity=intensity, edges=edges, texture=texture
            )
            for s in sigmas
        ]
        features = itertools.chain.from_iterable(
            dask.compute(*out_sigmas, num_workers=num_workers)
        )
    else:
        with ThreadPoolExecutor(max_workers=num_workers) as ex:
            out_sigmas = list(
                ex.map(
                    lambda s: _singlescale_basic_features_singlechannel(
                        img, s, intensity=intensity, edges=edges, texture=texture
                    ),
                    sigmas,
                )
            )
        features = itertools.chain.from_iterable(out_sigmas)
    return features


def multiscale_basic_features(
    image,
    multichannel=True,
    intensity=True,
    edges=True,
    texture=True,
    sigma_min=0.5,
    sigma_max=16,
    num_workers=None,
):
    """Local features for a single- or multi-channel nd image.

    Intensity, gradient intensity and local structure are computed at
    different scales thanks to Gaussian blurring.

    Parameters
    ----------
    image : ndarray
        Input image, which can be grayscale or multichannel.
    multichannel : bool, default False
        True if the last dimension corresponds to color channels.
    intensity : bool, default True
        If True, pixel intensities averaged over the different scales
        are added to the feature set.
    edges : bool, default True
        If True, intensities of local gradients averaged over the different
        scales are added to the feature set.
    texture : bool, default True
        If True, eigenvalues of the Hessian matrix after Gaussian blurring
        at different scales are added to the feature set.
    sigma_min : float, optional
        Smallest value of the Gaussian kernel used to average local
        neighbourhoods before extracting features.
    sigma_max : float, optional
        Largest value of the Gaussian kernel used to average local
        neighbourhoods before extracting features.
    num_workers : int or None, optional
        The number of parallel threads to use. If set to ``None``, the full
        set of available cores are used


    Returns
    -------
    features : np.ndarray
        Array of shape ``(n_features,) + image.shape``
    """
    if image.ndim >= 3 and multichannel:
        all_results = (
            _mutiscale_basic_features_singlechannel(
                image[..., dim],
                intensity=intensity,
                edges=edges,
                texture=texture,
                sigma_min=sigma_min,
                sigma_max=sigma_max,
                num_workers=num_workers,
            )
            for dim in range(image.shape[-1])
        )
        features = list(itertools.chain.from_iterable(all_results))
    else:
        features = list(_mutiscale_basic_features_singlechannel(
            image,
            intensity=intensity,
            edges=edges,
            texture=texture,
            sigma_min=sigma_min,
            sigma_max=sigma_max,
            num_workers=num_workers,
        ))
    return np.array(features, dtype=np.float32)


class TrainableSegmenter(object):
    """
    Estimator for classifying pixels.

    Parameters
    ----------
    clf : classifier object, optional
        classifier object, exposing a ``fit`` and a ``predict`` method as in
        scikit-learn's API, for example an instance of
        ``RandomForestClassifier`` or ``LogisticRegression`` classifier.
    features_func : function, optional
        function computing features on all pixels of the image, to be passed
        to the classifier. The output should be of shape
        ``(m_features, *labels.shape)``. If None,
        :func:`skimage.segmentation.multiscale_basic_features` is used.

    Methods
    -------
    compute_features
    fit
    predict
    """

    def __init__(self, clf=None, features_func=None):
        if clf is None:
            try:
                from sklearn.ensemble import RandomForestClassifier

                self.clf = RandomForestClassifier(n_estimators=100, n_jobs=-1)
            except ImportError:
                raise ImportError(
                    "Please install scikit-learn or pass a classifier instance"
                    "to TrainableSegmenter."
                )
        else:
            self.clf = clf
        self.features_func = features_func
        self.features = None

    def compute_features(self, image):
        if self.features_func is None:
            self.features_func = multiscale_basic_features
        self.features = self.features_func(image)

    def fit(self, labels, image=None):
        """
        Train classifier using partially labeled (annotated) image.

        Parameters
        ----------
        labels : ndarray of ints
            Labeled array of shape compatible with ``image`` (same shape for a
            single-channel image). Labels >= 1 correspond to the training set and
            label 0 to unlabeled pixels to be segmented.
        image : ndarray
            Input image, which can be grayscale or multichannel, and must have a
            number of dimensions compatible with ``self.features_func``.
        """
        if self.features is None:
            self.compute_features(image)
        output, clf = fit_segmenter(labels, self.features, self.clf)
        self.segmented_image = output

    def predict(self, image):
        """
        Segment new image using trained internal classifier.

        Parameters
        ----------
        image : ndarray
            Input image, which can be grayscale or multichannel, and must have a
            number of dimensions compatible with ``self.features_func``.

        Raises
        ------
        NotFittedError if ``self.clf`` has not been fitted yet (use ``self.fit``).
        """
        if self.features_func is None:
            self.features_func = multiscale_basic_features
        features = self.features_func(image)
        return predict_segmenter(features, self.clf)


def fit_segmenter(labels, features, clf):
    """
    Segmentation using labeled parts of the image and a classifier.

    Parameters
    ----------
    labels : ndarray of ints
        Image of labels. Labels >= 1 correspond to the training set and
        label 0 to unlabeled pixels to be segmented.
    features : ndarray
        Array of features, with the first dimension corresponding to the number
        of features, and the other dimensions correspond to ``labels.shape``.
    clf : classifier object
        classifier object, exposing a ``fit`` and a ``predict`` method as in
        scikit-learn's API, for example an instance of
        ``RandomForestClassifier`` or ``LogisticRegression`` classifier.

    Returns
    -------
    output : ndarray
        Labeled array, built from the prediction of the classifier trained on
        ``labels``.
    clf : classifier object
        classifier trained on ``labels``

    Raises
    ------
    NotFittedError if ``self.clf`` has not been fitted yet (use ``self.fit``).
    """
    training_data = features[:, labels > 0].T
    training_labels = labels[labels > 0].ravel()
    clf.fit(training_data, training_labels)
    data = features[:, labels == 0].T
    predicted_labels = clf.predict(data)
    output = np.copy(labels)
    output[labels == 0] = predicted_labels
    return output, clf


def predict_segmenter(features, clf):
    """
    Segmentation of images using a pretrained classifier.

    Parameters
    ----------
    features : ndarray
        Array of features, with the first dimension corresponding to the number
        of features, and the other dimensions are compatible with the shape of
        the image to segment.
    clf : classifier object
        trained classifier object, exposing a ``predict`` method as in
        scikit-learn's API, for example an instance of
        ``RandomForestClassifier`` or ``LogisticRegression`` classifier. The
        classifier must be already trained, for example with
        :func:`skimage.segmentation.fit_segmenter`.
    features_func : function, optional
        function computing features on all pixels of the image, to be passed
        to the classifier. The output should be of shape
        ``(m_features, *labels.shape)``. If None,
        :func:`skimage.segmentation.multiscale_basic_features` is used.

    Returns
    -------
    output : ndarray
        Labeled array, built from the prediction of the classifier.
    """
    sh = features.shape
    features = features.reshape((sh[0], np.prod(sh[1:]))).T
    try:
        predicted_labels = clf.predict(features)
    except NotFittedError:
        raise NotFittedError(
            "You must train the classifier `clf` first"
            "for example with the `fit_segmenter` function."
        )
    output = predicted_labels.reshape(sh[1:])
    return output
